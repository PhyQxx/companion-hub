"""Atomic tool admission and content-free, append-only outcome accounting."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, Database, TaskRunEventRecord, TaskRunRecord
from app.db.claims import assert_current_claim
from app.harness.budget import BudgetDenied, ToolPermit
from app.ids import uuid7
from app.schemas import PrivacyLevel

from .budget import RunModelBudget, utc
from .budget_origins import BudgetOrigin, require_budget_origins
from .store import append_run_event


def resource_usage(row: TaskRunRecord) -> dict[str, Any]:
    value = row.contract.get("resource_usage")
    return dict(value) if isinstance(value, dict) else {}


class RunToolBudget:
    def __init__(
        self,
        database: Database,
        *,
        run_id: UUID,
        user_id: UUID,
        config: RunBudgetConfig,
        maintenance: bool = False,
        deadline: datetime | None = None,
        allow_active_model_parent: bool | None = None,
        origins: tuple[BudgetOrigin, ...] = (),
    ) -> None:
        self._origins = origins
        self._database, self._run_id, self._user_id = database, run_id, user_id
        self._config, self._maintenance = config, maintenance
        # Tool maintenance may execute on an active root, while a model-origin
        # facade can still forbid model maintenance until that root completes.
        self._allow_active_model_parent = (
            maintenance if allow_active_model_parent is None else allow_active_model_parent
        )
        self._deadline = deadline or datetime.now(UTC) + timedelta(
            seconds=config.maintenance_deadline_seconds
        )

    @property
    def origins(self) -> tuple[BudgetOrigin, ...]:
        return self._origins

    @property
    def run_id(self) -> UUID:
        return self._run_id

    @property
    def owner_id(self) -> UUID:
        return self._user_id

    @property
    def budget_config(self) -> RunBudgetConfig:
        return self._config

    @property
    def maintenance(self) -> bool:
        return self._maintenance

    @property
    def allow_active_model_parent(self) -> bool:
        return self._allow_active_model_parent

    @property
    def delivery_deadline(self) -> datetime:
        return self._deadline

    async def _lock_run(self, session: AsyncSession) -> TaskRunRecord | None:
        # A no-op write gives SQLite the same serialization as PostgreSQL row locks.
        return await session.scalar(
            update(TaskRunRecord)
            .where(
                TaskRunRecord.id == self._run_id,
                TaskRunRecord.user_id == self._user_id,
            )
            .values(updated_at=TaskRunRecord.updated_at)
            .returning(TaskRunRecord)
        )

    async def reserve_tool(self, *, tool_name: str, user_id: UUID | None) -> ToolPermit:
        if user_id != self._user_id:
            raise BudgetDenied("budget_owner_invalid")
        await recover_tool_reservations(self._database, run_id=self._run_id, user_id=self._user_id)
        async with self._database.sessions.begin() as session:
            await assert_current_claim(session)
            # Match model admission's claim -> owner -> Run order and retain
            # the active-owner fence through acceptance, including on SQLite.
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
            row = await self._lock_run(session)
            if row is None:
                raise BudgetDenied("budget_run_not_found")
            now = datetime.now(UTC)
            allowed = (
                {"accepted", "running", "succeeded"}
                if self._maintenance
                else {"accepted", "running"}
            )
            if row.status not in allowed or row.contract.get("work_cancel_requested"):
                raise BudgetDenied("budget_run_inactive")
            if row.contract.get("budget_usage_overflow"):
                raise BudgetDenied("budget_usage_overflow")
            if not row.budget or not row.budget.get("enabled"):
                raise BudgetDenied("budget_snapshot_missing")
            deadline = self._deadline if self._maintenance else row.deadline
            if self._maintenance and row.status != "succeeded":
                if row.deadline is None or deadline is None:
                    raise BudgetDenied("run_deadline_exceeded")
                deadline = min(utc(deadline), utc(row.deadline))
            if deadline is None or utc(deadline) <= now:
                raise BudgetDenied("run_deadline_exceeded")
            usage = resource_usage(row)
            attempts = int(usage.get("tool_attempts", 0))
            limit = min(
                int(row.budget.get("max_tool_attempts", self._config.max_tool_attempts)),
                self._config.max_tool_attempts,
            )
            if attempts >= limit:
                raise BudgetDenied("tool_budget_exhausted")
            call_id = uuid7()
            row.contract = {
                **row.contract,
                "resource_usage": {**usage, "tool_attempts": attempts + 1},
            }
            row.state_version += 1
            row.updated_at = now
            await append_run_event(
                session,
                row,
                "run.tool.reserved",
                payload={
                    "call_id": str(call_id),
                    "tool_name": tool_name,
                    "deadline": utc(deadline).isoformat(),
                },
            )
            # Event allocation and its final ORM flush can wait on storage.
            # Reject expired acceptance here so counters/events roll back.
            await session.flush()
            await require_budget_origins(
                session, self._origins, run_id=self._run_id, user_id=self._user_id
            )
            if utc(deadline) <= datetime.now(UTC):
                raise BudgetDenied("run_deadline_exceeded")
        remaining = (utc(deadline) - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            await self.settle_tool(call_id, reported_ok=False)
            raise BudgetDenied("run_deadline_exceeded")
        return ToolPermit(call_id, remaining)

    async def settle_tool(self, call_id: UUID, *, reported_ok: bool | None) -> None:
        async with self._database.sessions.begin() as session:
            row = await self._lock_run(session)
            if row is None:
                return  # Deleted sources are never recreated by a late return.
            events = list(
                await session.scalars(
                    select(TaskRunEventRecord)
                    .where(
                        TaskRunEventRecord.run_id == self._run_id,
                        TaskRunEventRecord.payload["call_id"].as_string() == str(call_id),
                    )
                    .order_by(TaskRunEventRecord.seq)
                )
            )
            if not events or events[0].kind != "run.tool.reserved":
                raise BudgetDenied("tool_reservation_not_found")
            if len(events) > 1:
                if events[-1].payload.get("reported_ok") != reported_ok:
                    raise BudgetDenied("tool_settlement_conflict")
                return
            usage = resource_usage(row)
            counter = "unknown_tool_calls" if reported_ok is None else "returned_tool_calls"
            usage[counter] = int(usage.get(counter, 0)) + 1
            row.contract = {**row.contract, "resource_usage": usage}
            row.state_version += 1
            row.updated_at = datetime.now(UTC)
            await append_run_event(
                session,
                row,
                "run.tool.unknown" if reported_ok is None else "run.tool.returned",
                payload={"call_id": str(call_id), "reported_ok": reported_ok},
            )


async def plan_tool_budget(
    database: Database,
    run_id: UUID,
    user_id: UUID,
    deadline: datetime,
    current_config: RunBudgetConfig | None = None,
) -> RunToolBudget | None:
    from .chat_parent import require_chat_parent
    from .parent_budget import ParentBudgetScope

    deadline = utc(deadline)
    async with database.sessions() as session:
        row = await session.get(TaskRunRecord, run_id)
        if row is None or row.user_id != user_id:
            raise BudgetDenied("budget_run_not_found")
        if row.status not in {"accepted", "running", "succeeded"} or row.contract.get(
            "work_cancel_requested"
        ):
            raise BudgetDenied("budget_run_inactive")
        if row.contract.get("budget_usage_overflow"):
            raise BudgetDenied("budget_usage_overflow")
        origins: tuple[BudgetOrigin, ...] = ()
        raw_scope = row.contract.get("quota_scope")
        if (
            row.parent_run_id is not None
            or "budget_parent_id" in row.contract
            or "quota_scope" in row.contract
        ):
            if (
                row.parent_run_id is None
                or row.contract.get("budget_parent_id") != str(row.parent_run_id)
                or row.conversation_id is None
            ):
                raise BudgetDenied("chat_parent_changed")
            root = await require_chat_parent(
                session,
                row.parent_run_id,
                user_id=user_id,
                conversation_id=row.conversation_id,
                privacy_level=PrivacyLevel(row.privacy_level),
                child_id=row.id,
                quota_scope=raw_scope,
                allow_succeeded=True,
            )
            origins = (BudgetOrigin.capture(row),)
        else:
            root = row
        if raw_scope is not None:
            scope = ParentBudgetScope.read(raw_scope)
            config = current_config or scope.config
            budget = scope.restore(database, config, maintenance=True)
            origins = tuple(dict.fromkeys((*scope.origins, *origins)))
        else:
            if not root.budget or not root.budget.get("enabled"):
                return None
            frozen = RunBudgetConfig.model_validate(root.budget)
            config = current_config or frozen
            original = RunModelBudget(
                database,
                run_id=root.id,
                user_id=user_id,
                config=frozen,
                phase="maintenance",
                allow_active_parent=True,
                delivery_deadline=min(
                    deadline,
                    datetime.now(UTC) + timedelta(seconds=frozen.maintenance_deadline_seconds),
                ),
            )
            budget = ParentBudgetScope.capture(original).restore(database, config)
        await require_budget_origins(session, origins, run_id=budget.run_id, user_id=user_id)
    return RunToolBudget(
        database,
        run_id=budget.run_id,
        user_id=user_id,
        config=budget.budget_config,
        maintenance=True,
        allow_active_model_parent=budget.allow_active_parent,
        origins=origins,
        deadline=min(deadline, utc(budget.delivery_deadline)),
    )


async def recover_tool_reservations(
    database: Database,
    *,
    run_id: UUID | None = None,
    user_id: UUID | None = None,
) -> int:
    """Expired calls become unknown, without refunds or replay of external work."""
    after: UUID | None = None
    recovered = 0
    now = datetime.now(UTC)
    while True:
        query = (
            select(TaskRunRecord.id)
            .where(TaskRunRecord.contract["resource_usage"]["tool_attempts"].as_integer() > 0)
            .order_by(TaskRunRecord.id)
            .limit(100)
        )
        if run_id is not None:
            query = query.where(TaskRunRecord.id == run_id)
        if user_id is not None:
            query = query.where(TaskRunRecord.user_id == user_id)
        if after is not None:
            query = query.where(TaskRunRecord.id > after)
        async with database.sessions() as session:
            ids = list(await session.scalars(query))
        if not ids:
            return recovered
        for identifier in ids:
            async with database.sessions.begin() as session:
                row = await session.scalar(
                    update(TaskRunRecord)
                    .where(
                        TaskRunRecord.id == identifier,
                    )
                    .values(updated_at=TaskRunRecord.updated_at)
                    .returning(TaskRunRecord)
                )
                if row is None:
                    continue
                usage = resource_usage(row)
                if int(usage.get("tool_attempts", 0)) <= int(
                    usage.get("returned_tool_calls", 0)
                ) + int(usage.get("unknown_tool_calls", 0)):
                    continue
                events = list(
                    await session.scalars(
                        select(TaskRunEventRecord)
                        .where(
                            TaskRunEventRecord.run_id == identifier,
                            TaskRunEventRecord.kind.in_(
                                {"run.tool.reserved", "run.tool.returned", "run.tool.unknown"}
                            ),
                        )
                        .order_by(TaskRunEventRecord.seq)
                    )
                )
                settled = {
                    event.payload.get("call_id")
                    for event in events
                    if event.kind != "run.tool.reserved"
                }
                for event in events:
                    marker = event.payload.get("call_id")
                    if event.kind != "run.tool.reserved" or marker in settled:
                        continue
                    deadline = event.payload.get("deadline")
                    if not isinstance(deadline, str):
                        continue  # Missing evidence cannot justify an invented expiration.
                    try:
                        expiration = utc(datetime.fromisoformat(deadline))
                    except ValueError:
                        continue
                    if expiration > now:
                        continue
                    settled.add(marker)
                    usage["unknown_tool_calls"] = int(usage.get("unknown_tool_calls", 0)) + 1
                    row.contract = {**row.contract, "resource_usage": usage}
                    row.state_version += 1
                    row.updated_at = now
                    await append_run_event(
                        session,
                        row,
                        "run.tool.unknown",
                        payload={
                            "call_id": marker,
                            "reported_ok": None,
                            "reason_code": "tool_deadline_expired",
                        },
                    )
                    recovered += 1
        after = ids[-1]
