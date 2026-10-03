"""Atomic per-run model reservations, shared across routed provider attempts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .resources import RunToolBudget
from uuid import UUID

from sqlalchemy import func, select, update

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
from app.harness.budget import BudgetDenied, CallPermit
from app.harness.time import utc as utc
from app.ids import uuid7
from app.llm.contracts import ModelPricing, ModelUsage

from .costs import recover_cost_reservations, reserve_cost, settle_cost


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
    ) -> None:
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
    def tool_budget(self) -> RunToolBudget:
        from .resources import RunToolBudget

        return RunToolBudget(
            self._database,
            run_id=self._run_id,
            user_id=self._user_id,
            config=self._config,
            maintenance=self._phase == "maintenance",
            deadline=self._maintenance_deadline,
        )

    @property
    def run_id(self) -> UUID:
        return self._run_id

    @property
    def owner_id(self) -> UUID:
        return self._user_id

    @property
    def remaining_delivery_seconds(self) -> float:
        return max(0.0, (utc(self._maintenance_deadline) - datetime.now(UTC)).total_seconds())

    async def reserve(
        self, *, endpoint: str, tokens: int, final: bool, pricing: ModelPricing | None = None
    ) -> CallPermit:
        if tokens < 1:
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
            row = await session.scalar(
                select(TaskRunRecord).where(
                    TaskRunRecord.id == self._run_id, TaskRunRecord.user_id == self._user_id
                )
            )
            if row is None:
                raise BudgetDenied("budget_run_not_found")
            if not row.budget or not row.budget.get("enabled"):
                raise BudgetDenied("budget_snapshot_missing")
            max_attempts = min(int(row.budget["max_llm_attempts"]), self._config.max_llm_attempts)
            max_tokens = min(int(row.budget["max_tokens"]), self._config.max_tokens)
            deadline = row.deadline if self._phase == "interactive" else self._maintenance_deadline
            if deadline is None or utc(deadline) <= datetime.now(UTC):
                raise BudgetDenied("run_deadline_exceeded")
            # Preserve one model-attempt slot for a tool-free interactive answer.
            limit = max_attempts - (self._phase == "interactive" and not final)
            statuses = {"accepted", "running"} if self._phase == "interactive" else {"succeeded"}
            if self._phase == "maintenance" and self._allow_active_parent:
                statuses = {"accepted", "running", "succeeded"}
            if (
                self._phase == "maintenance"
                and self._allow_active_parent
                and row.status in {"accepted", "running"}
            ):
                limit = max_attempts - 1
            if row.status not in statuses or row.contract.get("work_cancel_requested"):
                raise BudgetDenied("budget_run_inactive")
            concurrency = min(
                int(
                    row.budget.get(
                        "max_concurrent_llm_calls", self._config.max_concurrent_llm_calls
                    )
                ),
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
            result = await session.execute(
                update(TaskRunRecord)
                .where(
                    TaskRunRecord.id == self._run_id,
                    TaskRunRecord.user_id == self._user_id,
                    TaskRunRecord.status.in_(statuses),
                    TaskRunRecord.contract["work_cancel_requested"].as_boolean().is_not(True),
                    TaskRunRecord.llm_attempts < limit,
                    TaskRunRecord.budget_tokens <= max_tokens - tokens,
                )
                .values(
                    llm_attempts=TaskRunRecord.llm_attempts + 1,
                    budget_tokens=TaskRunRecord.budget_tokens + tokens,
                )
                .returning(TaskRunRecord.id, TaskRunRecord.deadline)
                .execution_options(synchronize_session=False)
            )
            admitted = result.one_or_none()
            if admitted is None:
                await session.refresh(row)
                if row.status not in statuses or row.contract.get("work_cancel_requested"):
                    raise BudgetDenied("budget_run_inactive")
                raise BudgetDenied("run_budget_exhausted")
            if self._phase == "interactive":
                deadline = admitted.deadline
                if deadline is None:
                    raise BudgetDenied("run_deadline_exceeded")
            # Owner/Run UPDATEs can wait across a deadline or a UTC spending
            # boundary. Time before those locks cannot date an accepted call.
            now = datetime.now(UTC)
            if utc(deadline) <= now:
                raise BudgetDenied("run_deadline_exceeded")
            snapshot = RunBudgetConfig.model_validate(row.budget)
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
            checked_at = datetime.now(UTC)
            if utc(deadline) <= checked_at:
                raise BudgetDenied("run_deadline_exceeded")
            # No provider has received a permit. If flushing crosses a capped
            # period, roll back rather than commit an entry in the wrong one.
            for policy in (snapshot, self._config):
                if (policy.max_daily_cost is not None and now.date() != checked_at.date()) or (
                    policy.max_monthly_cost is not None
                    and (now.year, now.month) != (checked_at.year, checked_at.month)
                ):
                    raise BudgetDenied("cost_window_changed")
        # Include database admission latency in the remaining execution time.
        remaining = (utc(deadline) - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            await self._settle(
                call_id,
                ModelUsage(input_tokens=0, output_tokens=0, total_tokens=0, usage_known=True),
                provider_not_started=True,
            )
            raise BudgetDenied("run_deadline_exceeded")
        return CallPermit(call_id, remaining)

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
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
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
                await settle_cost(
                    session, call_id=call_id, user_id=self._user_id, usage=usage, now=now
                )
            # Match source deletion/recovery lock order before changing usage.
            run = await session.scalar(
                select(TaskRunRecord)
                .where(TaskRunRecord.id == self._run_id, TaskRunRecord.user_id == self._user_id)
                .with_for_update()
            )
            if run is None:
                return
            record = await session.scalar(
                select(ModelReservationRecord)
                .join(TaskRunRecord, TaskRunRecord.id == ModelReservationRecord.run_id)
                .where(
                    ModelReservationRecord.call_id == call_id,
                    ModelReservationRecord.run_id == self._run_id,
                    TaskRunRecord.user_id == self._user_id,
                )
            )
            if record is None:
                # Source deletion also deletes budget records; do not recreate them.
                return
            if (
                usage is None
                or usage.usage_known is False
                or (actual == 0 and usage.usage_known is not True)
            ):
                await session.execute(
                    update(ModelReservationRecord)
                    .where(
                        ModelReservationRecord.call_id == call_id,
                        ModelReservationRecord.state == "reserved",
                    )
                    .values(state="unknown", settled_at=now)
                )
                return
            admitted = await session.scalar(
                update(ModelReservationRecord)
                .where(
                    ModelReservationRecord.call_id == call_id,
                    ModelReservationRecord.state.in_({"reserved", "unknown"}),
                )
                .values(
                    state="settled", actual_tokens=actual, charged_tokens=actual, settled_at=now
                )
                .returning(ModelReservationRecord.call_id)
                .execution_options(synchronize_session=False)
            )
            if admitted is not None:
                await session.execute(
                    update(TaskRunRecord)
                    .where(TaskRunRecord.id == self._run_id)
                    .values(
                        budget_tokens=TaskRunRecord.budget_tokens + actual - record.reserved_tokens
                    )
                )
            else:
                await session.refresh(record)
                if record.actual_tokens != actual:
                    raise BudgetDenied("budget_settlement_conflict")


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
            if (job.input or {}).get("turn_id"):
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
