"""Owned provider operations; returned transport evidence is not business success."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, TypeVar, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, Database, ModelCostRecord, TaskRunRecord
from app.db.claims import assert_current_claim
from app.harness.budget import BudgetDenied, budget_scope, current_budget
from app.harness.guarded_call import guarded_call, guarded_inline_call
from app.harness.operations import OperationPolicy
from app.harness.source_cleanup import close_after_source
from app.harness.time import utc
from app.harness.unit_costs import unit_charge
from app.ids import uuid7
from app.schemas import PrivacyLevel

from .budget import RunModelBudget
from .costs import assert_cost_window, check_cost_allowance
from .store import append_run_event, transition_run
from .unit_costs import lock_unit_cost, reserve_unit_cost, settle_unit_cost

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
    source_commit_guard: Callable[[], Awaitable[None]] | None = None,
    cost_endpoint: str,
    budget_source: Callable[[], RunBudgetConfig] | None = None,
    cooperative: bool = False,
    trace_parent_id: UUID | None = None,
    missing_quote_reason: Literal[
        "media_cost_estimate_unavailable", "voice_cost_estimate_unavailable"
    ] = "media_cost_estimate_unavailable",
) -> T:
    """Own the call, accounting and terminal writes.

    An optional source_commit_guard rechecks revocable in-memory authority after
    SQL lock/fee waits. It must not acquire SQL or other external resources while
    the owner/run locks are held; ordinary source_guard runs outside those locks.
    """
    if privacy_level == PrivacyLevel.L3:
        raise BudgetDenied("ephemeral_operation_run_forbidden")
    parent = current_budget()
    if parent is not None and (
        not isinstance(parent, RunModelBudget) or parent.owner_id != user_id
    ):
        raise BudgetDenied("budget_owner_invalid")
    if isinstance(parent, RunModelBudget):
        parent = RunModelBudget(
            database,
            run_id=parent.run_id,
            user_id=parent.owner_id,
            config=parent.budget_config.model_copy(deep=True),
            phase=parent.phase,
            allow_active_parent=parent.allow_active_parent,
            delivery_deadline=parent.delivery_deadline,
        )
    quota_parent_id = parent.run_id if isinstance(parent, RunModelBudget) else None
    parent_id = trace_parent_id or quota_parent_id
    parent_ids = tuple(dict.fromkeys(value for value in (quota_parent_id, parent_id) if value))
    config = RunBudgetConfig.model_validate(dict(policy.budget))
    quote = policy.unit_quote
    parent_config = parent.budget_config if isinstance(parent, RunModelBudget) else config
    admitted_at: datetime | None = None
    run_id = uuid7()
    now = datetime.now(UTC)
    deadline = now + timedelta(seconds=config.maintenance_deadline_seconds)
    if isinstance(parent, RunModelBudget):
        deadline = min(deadline, utc(parent.delivery_deadline))

    def validate_rows(rows: list[TaskRunRecord]) -> None:
        current_time = datetime.now(UTC)
        if deadline <= current_time:
            raise BudgetDenied("run_deadline_exceeded")
        current_config = budget_source() if budget_source is not None else config
        if quote is None and any(
            candidate.max_daily_cost is not None or candidate.max_monthly_cost is not None
            for candidate in (config, current_config, parent_config)
        ):
            # Media cannot bypass a monetary cap before unit pricing exists.
            raise BudgetDenied(missing_quote_reason)
        for row in rows:
            completed_parent = (
                row.id == quota_parent_id
                and isinstance(parent, RunModelBudget)
                and parent.phase == "maintenance"
                and row.status == "succeeded"
            )
            if row.id == quota_parent_id and (
                not row.budget or row.budget.get("enabled") is not True
            ):
                raise BudgetDenied("budget_snapshot_missing")
            if row.id == quota_parent_id and not completed_parent and row.deadline is None:
                raise BudgetDenied("run_deadline_exceeded")
            if (
                row.user_id != user_id
                or (row.status not in {"accepted", "running"} and not completed_parent)
                or row.contract.get("work_cancel_requested")
            ):
                raise BudgetDenied("budget_run_inactive")
            if row.contract.get("budget_usage_overflow"):
                raise BudgetDenied("budget_usage_overflow")
            if int(str(row.privacy_level)[1]) > int(str(privacy_level)[1]):
                raise BudgetDenied("operation_privacy_downgrade")
            if (
                not completed_parent
                and row.deadline is not None
                and utc(row.deadline) <= current_time
            ):
                raise BudgetDenied("run_deadline_exceeded")
            if (
                quote is None
                and row.budget
                and (
                    row.budget.get("max_daily_cost") is not None
                    or row.budget.get("max_monthly_cost") is not None
                )
            ):
                raise BudgetDenied(missing_quote_reason)

    async def check_fees(
        session: AsyncSession, rows: list[TaskRunRecord], *, accepting: bool
    ) -> None:
        if quote is None:
            return
        amount = (
            unit_charge(quote.pricing.rate_per_unit, quote.maximum_quantity) if accepting else 0
        )
        current = budget_source() if budget_source is not None else config
        snapshots = [
            config,
            parent_config,
            *(RunBudgetConfig.model_validate(row.budget) for row in rows if row.budget is not None),
        ]
        if admitted_at is not None:
            assert_cost_window(admitted_at, datetime.now(UTC), (*snapshots, current))
        for snapshot in snapshots:
            await check_cost_allowance(
                session,
                user_id=user_id,
                amount=amount,
                currency=quote.pricing.currency,
                config=current,
                snapshot=snapshot,
                now=datetime.now(UTC),
            )

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
        for target in (*parent_ids, identifier):
            if target is None:
                continue
            row = await lock_run(session, target)
            if row is None:
                raise BudgetDenied("budget_run_inactive")
            rows.append(row)
        # A later lock wait can expire the previously locked parent's deadline.
        validate_rows(rows)
        await check_fees(session, rows, accepting=identifier is None)
        validate_rows(rows)
        return rows

    async def check(identifier: UUID | None) -> None:
        async with database.sessions() as session:
            await assert_current_claim(session)
            owner = await session.get(AppUserRecord, user_id)
            if owner is None or owner.status != "active":
                raise BudgetDenied("budget_owner_invalid")
            rows = []
            for target in (*parent_ids, identifier):
                if target is None:
                    continue
                row = await session.get(TaskRunRecord, target)
                if row is None:
                    raise BudgetDenied("budget_run_inactive")
                rows.append(row)
            validate_rows(rows)
            await check_fees(session, rows, accepting=False)
            validate_rows(rows)
        await source_guard()

    await check(None)
    await recover_expired_operations(database)
    async with database.sessions.begin() as session:
        authority = await lock_authority(session)
        if quote is not None:
            admitted_at = await reserve_unit_cost(
                session,
                call_id=run_id,
                user_id=user_id,
                endpoint=cost_endpoint,
                quote=quote,
                config=budget_source() if budget_source is not None else config,
                snapshot=config,
                now=datetime.now(UTC),
                clock=lambda: datetime.now(UTC),
            )
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
                budget=config.model_dump(mode="json") if parent is None or quote else None,
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
        await check_fees(session, authority, accepting=False)
        validate_rows(authority)
        if source_commit_guard is not None:
            await source_commit_guard()
    budget = parent or (
        RunModelBudget(database, run_id=run_id, user_id=user_id, config=config)
        if config.enabled and trace_parent_id is None
        else None
    )
    started = False
    unissued_after_start = False

    async def release_untransferred_quote() -> None:
        nonlocal unissued_after_start
        if quote is None:
            return
        amount = unit_charge(quote.pricing.rate_per_unit, quote.maximum_quantity)
        async with database.sessions.begin() as session:
            released = await session.scalar(
                update(ModelCostRecord)
                .where(
                    ModelCostRecord.call_id == run_id,
                    ModelCostRecord.user_id == user_id,
                    ModelCostRecord.unit == quote.pricing.unit,
                    ModelCostRecord.unit_rate == quote.pricing.rate_per_unit,
                    ModelCostRecord.unit_maximum_quantity == quote.maximum_quantity,
                    ModelCostRecord.currency == quote.pricing.currency,
                    ModelCostRecord.endpoint == cost_endpoint,
                    ModelCostRecord.state == "unknown",
                    ModelCostRecord.unit_quantity.is_(None),
                    ModelCostRecord.provider_request_id.is_(None),
                    ModelCostRecord.charged_micros == amount,
                    ModelCostRecord.reserved_micros == amount,
                )
                .values(
                    state="estimated",
                    charged_micros=0,
                    unit_quantity=0,
                    settled_at=datetime.now(UTC),
                )
                .returning(ModelCostRecord.call_id)
            )
            unissued_after_start = released is not None

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
            if quote is None:
                session.add(
                    ModelCostRecord(
                        call_id=run_id,
                        user_id=user_id,
                        endpoint=cost_endpoint,
                        state="unknown",
                        created_at=datetime.now(UTC),
                    )
                )
            else:
                cost = await lock_unit_cost(session, call_id=run_id, user_id=user_id)
                if (
                    cost is None
                    or cost.state != "reserved"
                    or cost.unit != quote.pricing.unit
                    or cost.currency != quote.pricing.currency
                    or cost.unit_rate != quote.pricing.rate_per_unit
                    or cost.unit_maximum_quantity != quote.maximum_quantity
                    or cost.endpoint != cost_endpoint
                ):
                    raise BudgetDenied("unit_cost_reservation_inactive")
                cost.state = "unknown"
            row.state_version += 1
            row.updated_at = datetime.now(UTC)
            await append_run_event(session, row, "run.provider.started")
            await session.flush()
            validate_rows(authority)
            await check_fees(session, authority, accepting=False)
            validate_rows(authority)
            if source_commit_guard is not None:
                await source_commit_guard()
        started = True
        if quote is not None:
            try:
                await check(run_id)
            except BaseException:
                # The start callback has not returned to the provider adapter.
                # Release only our untouched quote; any receipt/usage keeps its fee.
                async with close_after_source(release_untransferred_quote):
                    raise

    try:
        with budget_scope(budget):
            await check(run_id)
            async with asyncio.timeout(max(0, (deadline - datetime.now(UTC)).total_seconds())):
                guard = guarded_inline_call if cooperative else guarded_call
                result = await guard(lambda: invoke(mark_started), lambda: check(run_id))
            async with database.sessions.begin() as session:
                authority = await lock_authority(session, run_id)
                row = authority[-1]
                if row.status != "running":
                    raise BudgetDenied("budget_run_inactive")
                returned_evidence = evidence(result)
                row.contract = {**row.contract, **returned_evidence, "operation_state": "returned"}
                cost = (
                    await lock_unit_cost(session, call_id=run_id, user_id=user_id)
                    if quote is not None
                    else await session.get(ModelCostRecord, run_id)
                )
                if quote is not None and cost is None:
                    raise BudgetDenied("unit_cost_reservation_inactive")
                if cost is not None:
                    if quote is not None:
                        await settle_unit_cost(
                            session,
                            call_id=run_id,
                            user_id=user_id,
                            usage=None,
                            now=datetime.now(UTC),
                        )
                    receipt = returned_evidence.get("provider_request_id")
                    if (
                        quote is not None
                        and cost.provider_request_id
                        and receipt
                        and (receipt != cost.provider_request_id)
                    ):
                        raise BudgetDenied("cost_receipt_conflict")
                    if receipt is not None:
                        cost.provider_request_id = receipt
                    cost.settled_at = datetime.now(UTC)
                await transition_run(session, run_id, "succeeded")
                await session.flush()
                # The terminal child is intentionally excluded from active-state
                # validation; its time limit and parent authority still apply.
                validate_rows(authority[:-1])
                await check_fees(session, authority, accepting=False)
                validate_rows(authority[:-1])
                if row.deadline and utc(row.deadline) <= datetime.now(UTC):
                    raise BudgetDenied("run_deadline_exceeded")
                if source_commit_guard is not None:
                    await source_commit_guard()
            return result
    except BaseException as error:
        async with database.sessions.begin() as session:
            failed_row = await lock_run(session, run_id)
            if failed_row is not None:
                if quote is not None:
                    # Only a reservation still known to be unissued can release.
                    await session.execute(
                        update(ModelCostRecord)
                        .where(
                            ModelCostRecord.call_id == run_id,
                            ModelCostRecord.user_id == user_id,
                            ModelCostRecord.unit.is_not(None),
                            ModelCostRecord.state == "reserved",
                        )
                        .values(
                            state="estimated",
                            charged_micros=0,
                            unit_quantity=0,
                            settled_at=datetime.now(UTC),
                        )
                    )
                failed_row.contract = {
                    **failed_row.contract,
                    "operation_state": "unknown"
                    if not unissued_after_start
                    and (started or failed_row.contract.get("operation_state") == "started")
                    else "not_started",
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
        # Advisory inventory is outside the write transaction. SQLite ignores
        # FOR UPDATE; upgrading a read transaction races live start/recovery.
        async with database.sessions() as reader:
            candidates = (
                await reader.execute(
                    select(TaskRunRecord.id, TaskRunRecord.user_id)
                    .where(
                        TaskRunRecord.status.in_({"accepted", "running"}),
                        TaskRunRecord.deadline <= datetime.now(UTC),
                        TaskRunRecord.contract["criterion"].as_string()
                        == "provider_response_returned",
                    )
                    .order_by(TaskRunRecord.id)
                    .limit(50)
                )
            ).all()
        if not candidates:
            return count
        rows: list[TaskRunRecord] = []
        async with database.sessions.begin() as session:
            for identifier, owner in candidates:
                row = cast(
                    TaskRunRecord | None,
                    await session.scalar(
                        update(TaskRunRecord)
                        .where(
                            TaskRunRecord.id == identifier,
                            TaskRunRecord.user_id == owner,
                            TaskRunRecord.status.in_({"accepted", "running"}),
                            TaskRunRecord.deadline <= datetime.now(UTC),
                            TaskRunRecord.contract["criterion"].as_string()
                            == "provider_response_returned",
                        )
                        .values(updated_at=TaskRunRecord.updated_at)
                        .returning(TaskRunRecord)
                        .execution_options(synchronize_session=False, populate_existing=True)
                    ),
                )
                if row is None:
                    continue
                rows.append(row)
                if row.contract.get("operation_state") != "started":
                    await session.execute(
                        update(ModelCostRecord)
                        .where(
                            ModelCostRecord.call_id == row.id,
                            ModelCostRecord.user_id == row.user_id,
                            ModelCostRecord.unit.is_not(None),
                            ModelCostRecord.state == "reserved",
                        )
                        .values(
                            state="estimated",
                            charged_micros=0,
                            unit_quantity=0,
                            settled_at=datetime.now(UTC),
                        )
                    )
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
        if len(candidates) < 50:
            return count
