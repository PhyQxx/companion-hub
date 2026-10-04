"""Atomic per-run model reservations, shared across routed provider attempts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, NoReturn

if TYPE_CHECKING:
    from .resources import RunToolBudget
from uuid import UUID

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from app.config.models import RunBudgetConfig
from app.db import (
    AppUserRecord,
    Database,
    JobRecord,
    ModelCostRecord,
    ModelReservationRecord,
    TaskRunRecord,
)
from app.db.claims import assert_current_claim
from app.harness.budget import MAX_MODEL_TOKENS, BudgetDenied, CallPermit
from app.harness.source_cleanup import close_after_source
from app.harness.time import utc as utc
from app.ids import uuid7
from app.llm.contracts import ModelPricing, ModelUsage

from .budget_origins import BudgetOrigin, require_budget_origins
from .costs import assert_cost_window, recover_cost_reservations, reserve_cost, settle_cost


def _bounded(value: ColumnElement[int], maximum: int) -> ColumnElement[int]:
    return case((value < maximum, value), else_=maximum)


class RunModelBudget:
    def __init__(
        self,
        database: Database,
        *,
        run_id: UUID,
        user_id: UUID,
        config: RunBudgetConfig,
        phase: Literal["interactive", "maintenance"] = "interactive",
        allow_active_parent: bool = False,
        delivery_deadline: datetime | None = None,
        origins: tuple[BudgetOrigin, ...] = (),
    ) -> None:
        self._origins = origins
        self._database = database
        self._run_id = run_id
        self._user_id = user_id
        self._config = config
        self._phase = phase
        self._allow_active_parent = allow_active_parent
        self._maintenance_deadline = delivery_deadline or (
            datetime.now(UTC) + timedelta(seconds=config.maintenance_deadline_seconds)
        )

    @property
    def origins(self) -> tuple[BudgetOrigin, ...]:
        return self._origins

    @property
    def tool_budget(self) -> RunToolBudget:
        from .resources import RunToolBudget

        return RunToolBudget(
            self._database,
            origins=self._origins,
            run_id=self._run_id,
            user_id=self._user_id,
            config=self._config,
            maintenance=self._phase == "maintenance",
            allow_active_model_parent=self._allow_active_parent,
            deadline=self._maintenance_deadline,
        )

    @property
    def run_id(self) -> UUID:
        return self._run_id

    @property
    def owner_id(self) -> UUID:
        return self._user_id

    @property
    def budget_config(self) -> RunBudgetConfig:
        """Frozen limits carried by this parent port, in addition to its Run snapshot."""
        return self._config

    @property
    def phase(self) -> Literal["interactive", "maintenance"]:
        return self._phase

    @property
    def allow_active_parent(self) -> bool:
        return self._allow_active_parent

    @property
    def delivery_deadline(self) -> datetime:
        return self._maintenance_deadline

    @property
    def remaining_delivery_seconds(self) -> float:
        return max(0.0, (utc(self._maintenance_deadline) - datetime.now(UTC)).total_seconds())

    async def reserve(
        self, *, endpoint: str, tokens: int, final: bool, pricing: ModelPricing | None = None
    ) -> CallPermit:
        if tokens < 1 or tokens > MAX_MODEL_TOKENS:
            raise ValueError("invalid_budget_reservation")
        async with self._database.sessions.begin() as session:
            await assert_current_claim(session)
            # Write before reading the snapshot: serialize cross-run owner
            # admission without a SQLite read-to-write transaction upgrade.
            owner = await session.scalar(
                update(AppUserRecord)
                .where(AppUserRecord.id == self._user_id, AppUserRecord.status == "active")
                .values(id=AppUserRecord.id)
                .returning(AppUserRecord.id)
            )
            if owner is None:
                owned_run = await session.scalar(
                    select(TaskRunRecord.id).where(
                        TaskRunRecord.id == self._run_id, TaskRunRecord.user_id == self._user_id
                    )
                )
                if owned_run is None:
                    raise BudgetDenied("budget_run_not_found")
                raise BudgetDenied("budget_owner_invalid")
            await require_budget_origins(
                session, self._origins, run_id=self._run_id, user_id=self._user_id, lock=True
            )
            statuses = {"accepted", "running"} if self._phase == "interactive" else {"succeeded"}
            if self._phase == "maintenance" and self._allow_active_parent:
                statuses = {"accepted", "running", "succeeded"}
            snapshot_attempts = TaskRunRecord.budget["max_llm_attempts"].as_integer()
            snapshot_tokens = TaskRunRecord.budget["max_tokens"].as_integer()
            limit = _bounded(snapshot_attempts, self._config.max_llm_attempts)
            if self._phase == "interactive" and not final:
                limit = limit - 1
            elif self._phase == "maintenance" and self._allow_active_parent:
                limit = limit - case(
                    (TaskRunRecord.status.in_({"accepted", "running"}), 1), else_=0
                )
            concurrency = _bounded(
                func.coalesce(
                    TaskRunRecord.budget["max_concurrent_llm_calls"].as_integer(),
                    self._config.max_concurrent_llm_calls,
                ),
                self._config.max_concurrent_llm_calls,
            )
            owned_runs = aliased(TaskRunRecord)
            in_flight = (
                select(func.count())
                .select_from(ModelReservationRecord)
                .join(owned_runs, owned_runs.id == ModelReservationRecord.run_id)
                .where(
                    owned_runs.user_id == self._user_id,
                    ModelReservationRecord.state == "reserved",
                )
                .scalar_subquery()
            )
            criteria = [
                TaskRunRecord.id == self._run_id,
                TaskRunRecord.user_id == self._user_id,
                TaskRunRecord.budget["enabled"].as_boolean().is_(True),
                snapshot_attempts.is_not(None),
                snapshot_tokens.is_not(None),
                TaskRunRecord.status.in_(statuses),
                TaskRunRecord.contract["work_cancel_requested"].as_boolean().is_not(True),
                TaskRunRecord.contract["budget_usage_overflow"].as_boolean().is_not(True),
                TaskRunRecord.llm_attempts < limit,
                TaskRunRecord.budget_tokens
                <= _bounded(snapshot_tokens, self._config.max_tokens) - tokens,
                in_flight < concurrency,
            ]
            if self._phase == "interactive":
                criteria.append(TaskRunRecord.deadline > datetime.now(UTC))
            elif utc(self._maintenance_deadline) <= datetime.now(UTC):
                raise BudgetDenied("run_deadline_exceeded")
            elif self._allow_active_parent:
                criteria.append(
                    or_(
                        TaskRunRecord.status == "succeeded",
                        TaskRunRecord.deadline > datetime.now(UTC),
                    )
                )
            result = await session.execute(
                update(TaskRunRecord)
                .where(*criteria)
                .values(
                    llm_attempts=TaskRunRecord.llm_attempts + 1,
                    budget_tokens=TaskRunRecord.budget_tokens + tokens,
                )
                .returning(
                    TaskRunRecord.id,
                    TaskRunRecord.deadline,
                    TaskRunRecord.budget,
                    TaskRunRecord.status,
                )
                .execution_options(synchronize_session=False)
            )
            admitted = result.one_or_none()
            if admitted is None:
                await self._deny_admission(session, statuses)
            assert admitted is not None
            deadline = (
                admitted.deadline if self._phase == "interactive" else self._maintenance_deadline
            )
            if (
                self._phase == "maintenance"
                and self._allow_active_parent
                and admitted.status != "succeeded"
            ):
                if admitted.deadline is None:
                    raise BudgetDenied("run_deadline_exceeded")
                deadline = min(utc(deadline), utc(admitted.deadline))
            if deadline is None:
                raise BudgetDenied("run_deadline_exceeded")
            # Owner/Run UPDATEs can wait across a deadline or a UTC spending
            # boundary. Time before those locks cannot date an accepted call.
            now = datetime.now(UTC)
            if utc(deadline) <= now:
                raise BudgetDenied("run_deadline_exceeded")
            snapshot = RunBudgetConfig.model_validate(admitted.budget)
            call_id = uuid7()
            await reserve_cost(
                session,
                call_id=call_id,
                user_id=self._user_id,
                endpoint=endpoint,
                tokens=tokens,
                pricing=pricing,
                config=self._config,
                snapshot=snapshot,
                now=now,
            )
            session.add(
                ModelReservationRecord(
                    call_id=call_id,
                    run_id=self._run_id,
                    phase=self._phase,
                    endpoint=endpoint,
                    state="reserved",
                    reserved_tokens=tokens,
                    charged_tokens=tokens,
                    created_at=now,
                )
            )
            await session.flush()
            await require_budget_origins(
                session, self._origins, run_id=self._run_id, user_id=self._user_id
            )
            checked_at = datetime.now(UTC)
            if utc(deadline) <= checked_at:
                raise BudgetDenied("run_deadline_exceeded")
            # No provider has received a permit. If flushing crosses a capped
            # period, roll back rather than commit an entry in the wrong one.
            assert_cost_window(now, checked_at, (snapshot, self._config))

        # Include database admission latency in the remaining execution time.
        async def release_unissued() -> None:
            await self._settle(
                call_id,
                ModelUsage(input_tokens=0, output_tokens=0, total_tokens=0, usage_known=True),
                provider_not_started=True,
            )

        try:
            checked_at = datetime.now(UTC)
            remaining = (utc(deadline) - checked_at).total_seconds()
            if remaining <= 0:
                raise BudgetDenied("run_deadline_exceeded")
            assert_cost_window(now, checked_at, (snapshot, self._config))
        except BudgetDenied:
            async with close_after_source(release_unissued):
                raise
        return CallPermit(call_id, remaining)

    async def _deny_admission(self, session: AsyncSession, statuses: set[str]) -> NoReturn:
        # The successful path uses the snapshot in the conditional write.
        # Only a rejection needs these reads to classify the current reason.
        row = await session.scalar(
            select(TaskRunRecord).where(
                TaskRunRecord.id == self._run_id, TaskRunRecord.user_id == self._user_id
            )
        )
        if row is None:
            raise BudgetDenied("budget_run_not_found")
        if (
            not row.budget
            or not row.budget.get("enabled")
            or any(name not in row.budget for name in ("max_llm_attempts", "max_tokens"))
        ):
            raise BudgetDenied("budget_snapshot_missing")
        deadline = row.deadline if self._phase == "interactive" else self._maintenance_deadline
        if (
            self._phase == "maintenance"
            and self._allow_active_parent
            and row.status in {"accepted", "running"}
        ):
            if row.deadline is None or deadline is None:
                raise BudgetDenied("run_deadline_exceeded")
            deadline = min(utc(deadline), utc(row.deadline))
        if deadline is None or utc(deadline) <= datetime.now(UTC):
            raise BudgetDenied("run_deadline_exceeded")
        if row.status not in statuses or row.contract.get("work_cancel_requested"):
            raise BudgetDenied("budget_run_inactive")
        if row.contract.get("budget_usage_overflow"):
            raise BudgetDenied("budget_usage_overflow")
        concurrency = min(
            int(row.budget.get("max_concurrent_llm_calls", self._config.max_concurrent_llm_calls)),
            self._config.max_concurrent_llm_calls,
        )
        in_flight = await session.scalar(
            select(func.count())
            .select_from(ModelReservationRecord)
            .join(TaskRunRecord, TaskRunRecord.id == ModelReservationRecord.run_id)
            .where(
                TaskRunRecord.user_id == self._user_id,
                ModelReservationRecord.state == "reserved",
            )
        )
        if int(in_flight or 0) >= concurrency:
            raise BudgetDenied("user_model_concurrency_exhausted")
        raise BudgetDenied("run_budget_exhausted")

    async def settle(self, call_id: UUID, usage: ModelUsage | None) -> None:
        await self._settle(call_id, usage, provider_not_started=False)

    async def _settle(
        self,
        call_id: UUID,
        usage: ModelUsage | None,
        *,
        provider_not_started: bool,
    ) -> None:
        actual = (
            max(usage.total_tokens, usage.input_tokens + usage.output_tokens)
            if usage is not None and usage.usage_known is not False
            else 0
        )
        observed = max(usage.total_tokens, usage.input_tokens + usage.output_tokens) if usage else 0
        overflow = False
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            # Recovery owns Run before reservation/fee. Match that order to
            # avoid waiting on Run while holding the fee recovery needs.
            run = (
                await session.execute(
                    update(TaskRunRecord)
                    .where(TaskRunRecord.id == self._run_id, TaskRunRecord.user_id == self._user_id)
                    .values(id=TaskRunRecord.id)
                    .returning(
                        TaskRunRecord.id, TaskRunRecord.budget_tokens, TaskRunRecord.contract
                    )
                    .execution_options(synchronize_session=False)
                )
            ).one_or_none()
            if provider_not_started:
                # Only reserve's postcommit gate uses this private path: its
                # permit never left the budget, so even unpriced usage is zero.
                await session.execute(
                    update(ModelCostRecord)
                    .where(
                        ModelCostRecord.call_id == call_id,
                        ModelCostRecord.user_id == self._user_id,
                        ModelCostRecord.state.in_({"reserved", "unknown"}),
                        ModelCostRecord.provider_request_id.is_(None),
                    )
                    .values(state="estimated", charged_micros=0, settled_at=now)
                )
            else:
                overflow = await settle_cost(
                    session, call_id=call_id, user_id=self._user_id, usage=usage, now=now
                )
            if run is None:
                # Fees outlive deleted sources, but their Run is never recreated.
                pass
            elif overflow or observed > MAX_MODEL_TOKENS:
                # Do not put out-of-range counters into BIGINT or a fabricated
                # saturated "actual". Keep the hold and close this Run's model
                # admission permanently; late exact receipts may fix its fee.
                affected = await session.scalar(
                    update(ModelReservationRecord)
                    .where(
                        ModelReservationRecord.call_id == call_id,
                        ModelReservationRecord.run_id == self._run_id,
                        ModelReservationRecord.state.in_({"reserved", "unknown"}),
                    )
                    .values(state="unknown", settled_at=now)
                    .returning(ModelReservationRecord.call_id)
                )
                if affected is not None:
                    overflow = True
                    await session.execute(
                        update(TaskRunRecord)
                        .where(
                            TaskRunRecord.id == self._run_id, TaskRunRecord.user_id == self._user_id
                        )
                        .values(contract={**run.contract, "budget_usage_overflow": True})
                    )
                elif usage is not None and usage.usage_known is not False:
                    existing = (
                        await session.execute(
                            select(ModelReservationRecord.actual_tokens).where(
                                ModelReservationRecord.call_id == call_id,
                                ModelReservationRecord.run_id == self._run_id,
                            )
                        )
                    ).one_or_none()
                    if existing is not None and existing.actual_tokens != actual:
                        raise BudgetDenied("budget_settlement_conflict")
            elif (
                usage is None
                or usage.usage_known is False
                or (actual == 0 and usage.usage_known is not True)
            ):
                await session.execute(
                    update(ModelReservationRecord)
                    .where(
                        ModelReservationRecord.call_id == call_id,
                        ModelReservationRecord.run_id == self._run_id,
                        ModelReservationRecord.state == "reserved",
                    )
                    .values(state="unknown", settled_at=now)
                )
            else:
                reserved = await session.scalar(
                    update(ModelReservationRecord)
                    .where(
                        ModelReservationRecord.call_id == call_id,
                        ModelReservationRecord.run_id == self._run_id,
                        ModelReservationRecord.state.in_({"reserved", "unknown"}),
                    )
                    .values(
                        state="settled", actual_tokens=actual, charged_tokens=actual, settled_at=now
                    )
                    .returning(ModelReservationRecord.reserved_tokens)
                    .execution_options(synchronize_session=False)
                )
                if reserved is not None:
                    total = run.budget_tokens + actual - reserved
                    overflow = total > MAX_MODEL_TOKENS
                    await session.execute(
                        update(TaskRunRecord)
                        .where(
                            TaskRunRecord.id == self._run_id, TaskRunRecord.user_id == self._user_id
                        )
                        .values(
                            {"contract": {**run.contract, "budget_usage_overflow": True}}
                            if overflow
                            else {"budget_tokens": total}
                        )
                    )
                else:
                    # Only an already-settled/missing call needs a read to distinguish
                    # a replay from a conflict. Successful settlement uses writes.
                    existing = (
                        await session.execute(
                            select(ModelReservationRecord.actual_tokens).where(
                                ModelReservationRecord.call_id == call_id,
                                ModelReservationRecord.run_id == self._run_id,
                            )
                        )
                    ).one_or_none()
                    if existing is not None and existing.actual_tokens != actual:
                        raise BudgetDenied("budget_settlement_conflict")
        # Rejection must occur after commit so neither the receipt nor the
        # admission fence is erased by exception-driven transaction rollback.
        if overflow:
            raise BudgetDenied("budget_usage_overflow")


async def recover_stale_reservations(database: Database) -> None:
    """Preserve charges after a crash. Never release usage we cannot prove unused."""
    await recover_cost_reservations(database)
    async with database.sessions.begin() as session:
        await session.execute(
            update(ModelReservationRecord)
            .where(
                ModelReservationRecord.state == "reserved",
                ModelReservationRecord.created_at < datetime.now(UTC) - timedelta(seconds=1800),
            )
            .values(state="unknown", settled_at=datetime.now(UTC))
        )


async def job_model_budget(
    database: Database, job_id: UUID, config: RunBudgetConfig
) -> RunModelBudget | None:
    """Share a chat parent's quota; give standalone jobs a durable run on first delivery.

    Retry reuses the same counters. A deleted/cancelled parent cannot be recreated.
    Metadata contains no research topic, URL, text or provider credentials.
    """
    from app.runs.store import append_run_event

    now = datetime.now(UTC)
    async with database.sessions.begin() as session:
        job = await session.get(JobRecord, job_id, with_for_update=True)
        if job is None or job.status not in {"running", "admitted"}:
            raise BudgetDenied("budget_run_inactive")
        try:
            user_id = UUID(job.owner)
        except ValueError as error:
            raise BudgetDenied("budget_owner_invalid") from error
        if (job.input or {}).get("user_id") != str(user_id):
            raise BudgetDenied("budget_owner_invalid")
        run_id = job.task_run_id
        if run_id is None:
            # A sourced job whose run vanished is not an independent task.
            if (job.input or {}).get("turn_id") or (
                job.kind.startswith("chat.") and (job.input or {}).get("assistant_message_id")
            ):
                raise BudgetDenied("budget_run_not_found")
            run_id = job.id
            row = TaskRunRecord(
                id=run_id,
                user_id=user_id,
                status="running",
                privacy_level="L1",
                contract={
                    "entry": "delegated" if job.kind.startswith("deleg.") else "background",
                    "criterion": "handler_completed",
                },
                budget=config.model_dump(mode="json"),
                deadline=now + timedelta(seconds=config.maintenance_deadline_seconds),
                created_at=now,
                updated_at=now,
                state_version=1,
                event_seq=0,
                cancel_epoch=0,
            )
            session.add(row)
            await session.flush()
            await append_run_event(session, row, "run.running")
            job.task_run_id = run_id
            if not config.enabled:
                return None
        else:
            existing = await session.get(TaskRunRecord, run_id)
            if existing is None or existing.user_id != user_id:
                raise BudgetDenied("budget_run_not_found")
            if existing.status not in {"accepted", "running", "succeeded"}:
                raise BudgetDenied("budget_run_inactive")
            if not existing.budget or not existing.budget.get("enabled"):
                return None  # Pre-budget runs preserve their recorded opt-out.
    return RunModelBudget(
        database,
        run_id=run_id,
        user_id=user_id,
        config=config,
        phase="interactive" if run_id == job_id else "maintenance",
        allow_active_parent=True,
        delivery_deadline=utc(job.started_at or job.created_at)
        + timedelta(seconds=config.maintenance_deadline_seconds),
    )
