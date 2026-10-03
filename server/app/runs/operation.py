"""Owned provider operations; returned transport evidence is not business success."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TypeVar, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, Database, ModelCostRecord, TaskRunRecord
from app.db.claims import assert_current_claim
from app.harness.budget import BudgetDenied, budget_scope, current_budget
from app.harness.guarded_call import guarded_call
from app.harness.operations import OperationPolicy
from app.harness.time import utc
from app.ids import uuid7
from app.schemas import PrivacyLevel

from .budget import RunModelBudget
from .store import append_run_event, transition_run

T = TypeVar("T")


async def operate_with_run(
    database: Database,
    policy: OperationPolicy,
    *,
    user_id: UUID,
    privacy_level: PrivacyLevel,
    entry: str,
    invoke: Callable[[Callable[[], Awaitable[None]]], Awaitable[T]],
    evidence: Callable[[T], dict[str, str]],
    source_guard: Callable[[], Awaitable[None]],
    cost_endpoint: str,
) -> T:
    if privacy_level == PrivacyLevel.L3:
        raise BudgetDenied("ephemeral_operation_run_forbidden")
    parent = current_budget()
    if parent is not None and (
        not isinstance(parent, RunModelBudget) or parent.owner_id != user_id
    ):
        raise BudgetDenied("budget_owner_invalid")
    parent_id = parent.run_id if isinstance(parent, RunModelBudget) else None
    config = RunBudgetConfig.model_validate(dict(policy.budget))
    run_id = uuid7()
    now = datetime.now(UTC)
    deadline = now + timedelta(seconds=config.maintenance_deadline_seconds)

    def validate_rows(rows: list[TaskRunRecord]) -> None:
        current_time = datetime.now(UTC)
        if deadline <= current_time:
            raise BudgetDenied("run_deadline_exceeded")
        if config.max_daily_cost is not None or config.max_monthly_cost is not None:
            # Media cannot bypass a monetary cap before unit pricing exists.
            raise BudgetDenied("media_cost_estimate_unavailable")
        for row in rows:
            if (
                row.user_id != user_id
                or row.status not in {"accepted", "running"}
                or row.contract.get("work_cancel_requested")
            ):
                raise BudgetDenied("budget_run_inactive")
            if int(str(row.privacy_level)[1]) > int(str(privacy_level)[1]):
                raise BudgetDenied("operation_privacy_downgrade")
            if row.deadline is not None and utc(row.deadline) <= current_time:
                raise BudgetDenied("run_deadline_exceeded")
            if row.budget and (
                row.budget.get("max_daily_cost") is not None
                or row.budget.get("max_monthly_cost") is not None
            ):
                raise BudgetDenied("media_cost_estimate_unavailable")

    async def lock_run(session: AsyncSession, identifier: UUID) -> TaskRunRecord | None:
        return cast(
            TaskRunRecord | None,
            await session.scalar(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == identifier, TaskRunRecord.user_id == user_id)
                .values(updated_at=TaskRunRecord.updated_at)
                .returning(TaskRunRecord)
                .execution_options(synchronize_session=False, populate_existing=True)
            ),
        )

    async def lock_authority(
        session: AsyncSession, identifier: UUID | None = None
    ) -> list[TaskRunRecord]:
        await assert_current_claim(session)
        # Claim -> owner -> parent -> child matches provider/tool admission.
        # No-op writes also fence SQLite without upgrading an earlier read.
        owner = await session.scalar(
            update(AppUserRecord)
            .where(AppUserRecord.id == user_id, AppUserRecord.status == "active")
            .values(id=AppUserRecord.id)
            .returning(AppUserRecord.id)
        )
        if owner is None:
            raise BudgetDenied("budget_owner_invalid")
        rows = []
        for target in (parent_id, identifier):
            if target is None:
                continue
            row = await lock_run(session, target)
            if row is None:
                raise BudgetDenied("budget_run_inactive")
            rows.append(row)
        # A later lock wait can expire the previously locked parent's deadline.
        validate_rows(rows)
        return rows

    async def check(identifier: UUID | None) -> None:
        async with database.sessions() as session:
            await assert_current_claim(session)
            owner = await session.get(AppUserRecord, user_id)
            if owner is None or owner.status != "active":
                raise BudgetDenied("budget_owner_invalid")
            rows = []
            for target in (parent_id, identifier):
                if target is None:
                    continue
                row = await session.get(TaskRunRecord, target)
                if row is None:
                    raise BudgetDenied("budget_run_inactive")
                rows.append(row)
            validate_rows(rows)
        await source_guard()

    await check(None)
    await recover_expired_operations(database)
    async with database.sessions.begin() as session:
        authority = await lock_authority(session)
        session.add(
            TaskRunRecord(
                id=run_id,
                user_id=user_id,
                parent_run_id=parent_id,
                request_id=f"{entry}:{run_id}",
                status="accepted",
                privacy_level=str(privacy_level),
                config_version=policy.config_version,
                created_at=now,
                updated_at=now,
                budget=config.model_dump(mode="json") if parent is None else None,
                deadline=deadline,
                contract={
                    "entry": entry,
                    "criterion": "provider_response_returned",
                    "required_work": [],
                    "operation_state": "not_started",
                },
            )
        )
        await session.flush()
        await append_run_event(
            session, await session.get_one(TaskRunRecord, run_id), "run.accepted"
        )
        await transition_run(session, run_id, "running")
        await session.flush()
        validate_rows(authority)
    budget = parent or (
        RunModelBudget(database, run_id=run_id, user_id=user_id, config=config)
        if config.enabled
        else None
    )
    started = False

    async def mark_started() -> None:
        nonlocal started
        await check(run_id)
        if started:
            return
        async with database.sessions.begin() as session:
            authority = await lock_authority(session, run_id)
            row = authority[-1]
            if row.status != "running":
                raise BudgetDenied("budget_run_inactive")
            row.contract = {**row.contract, "operation_state": "started"}
            session.add(
                ModelCostRecord(
                    call_id=run_id,
                    user_id=user_id,
                    endpoint=cost_endpoint,
                    state="unknown",
                    created_at=datetime.now(UTC),
                )
            )
            row.state_version += 1
            row.updated_at = datetime.now(UTC)
            await append_run_event(session, row, "run.provider.started")
            await session.flush()
            validate_rows(authority)
        started = True

    try:
        with budget_scope(budget):
            await check(run_id)
            async with asyncio.timeout(max(0, (deadline - datetime.now(UTC)).total_seconds())):
                result = await guarded_call(lambda: invoke(mark_started), lambda: check(run_id))
            async with database.sessions.begin() as session:
                authority = await lock_authority(session, run_id)
                row = authority[-1]
                if row.status != "running":
                    raise BudgetDenied("budget_run_inactive")
                returned_evidence = evidence(result)
                row.contract = {**row.contract, **returned_evidence, "operation_state": "returned"}
                cost = await session.get(ModelCostRecord, run_id)
                if cost is not None:
                    cost.provider_request_id = returned_evidence.get("provider_request_id")
                    cost.settled_at = datetime.now(UTC)
                await transition_run(session, run_id, "succeeded")
                await session.flush()
                # The terminal child is intentionally excluded from active-state
                # validation; its time limit and parent authority still apply.
                validate_rows(authority[:-1])
                if row.deadline and utc(row.deadline) <= datetime.now(UTC):
                    raise BudgetDenied("run_deadline_exceeded")
            return result
    except BaseException as error:
        async with database.sessions.begin() as session:
            failed_row = await lock_run(session, run_id)
            if failed_row is not None:
                failed_row.contract = {
                    **failed_row.contract,
                    "operation_state": "unknown" if started else "not_started",
                }
                failed_row.state_version += 1
                failed_row.updated_at = datetime.now(UTC)
                await append_run_event(session, failed_row, "run.provider.interrupted")
                await transition_run(
                    session,
                    run_id,
                    "cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                )
        raise


async def recover_expired_operations(database: Database) -> int:
    """Interrupted provider operations are never replayed after their deadline."""
    count = 0
    while True:
        async with database.sessions.begin() as session:
            rows = list(
                await session.scalars(
                    select(TaskRunRecord)
                    .where(
                        TaskRunRecord.status.in_({"accepted", "running"}),
                        TaskRunRecord.deadline <= datetime.now(UTC),
                        TaskRunRecord.contract["criterion"].as_string()
                        == "provider_response_returned",
                    )
                    .order_by(TaskRunRecord.id)
                    .limit(50)
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                row.contract = {
                    **row.contract,
                    "operation_state": "unknown"
                    if row.contract.get("operation_state") == "started"
                    else "not_started",
                }
                await transition_run(session, row.id, "failed")
        count += len(rows)
        from .resources import recover_tool_reservations

        for row in rows:
            await recover_tool_reservations(database, run_id=row.id, user_id=row.user_id)
        if len(rows) < 50:
            return count
