"""One durable, bounded dispatch attempt per owned source; no automatic replay."""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast
from uuid import UUID

from pydantic import Field, TypeAdapter, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.models import RunBudgetConfig
from app.db import (
    AppUserRecord,
    CognitiveGoalRecord,
    DailyBriefRecord,
    DailyReviewRecord,
    Database,
    ModelCostRecord,
    ModelReservationRecord,
    TaskItemRecord,
    TaskRunRecord,
)
from app.db.claims import assert_current_claim
from app.harness.budget import BudgetDenied, budget_scope, current_budget, current_tool_budget
from app.schemas.common import PrivacyLevel, TokenName
from app.schemas.delivery_run import DeliveryRunOutcome

from .budget import RunModelBudget, utc
from .store import append_run_event, transition_run

SourceTable = type[DailyBriefRecord] | type[DailyReviewRecord] | type[TaskItemRecord]
SourceRow = DailyBriefRecord | DailyReviewRecord | TaskItemRecord
_CHANNELS: TypeAdapter[list[str]] = TypeAdapter(Annotated[list[TokenName], Field(max_length=16)])


async def _source_lock(
    session: AsyncSession,
    table: SourceTable,
    source_id: UUID,
    user_id: UUID,
    *,
    pending: bool = False,
) -> SourceRow | None:
    query = update(table).where(table.id == source_id, table.user_id == user_id)
    if pending:
        query = query.where(
            table.status.in_({"active", "firing"})
            if table is TaskItemRecord
            else table.status == "pending"
        )
    return cast(
        SourceRow | None,
        await session.scalar(query.values(updated_at=table.updated_at).returning(table)),
    )


async def _check_goal_sources(session: AsyncSession, source: SourceRow) -> None:
    from app.cognition.goal_sources import goal_visibility

    if isinstance(source, TaskItemRecord):
        return
    items = source.facts if isinstance(source, DailyBriefRecord) else source.items
    identifiers: set[UUID] = set()
    for item in items:
        if item.get("action") == "removed":
            continue
        reference = item.get("source", "")
        if isinstance(reference, str) and reference.startswith("goal:"):
            try:
                identifiers.add(UUID(reference[5:]))
            except ValueError as error:
                raise BudgetDenied("delivery_source_invalid") from error
    if not identifiers:
        return
    if len(identifiers) > 1000:
        raise BudgetDenied("delivery_source_invalid")
    allowed = set(
        await session.scalars(
            select(CognitiveGoalRecord.id).where(
                CognitiveGoalRecord.id.in_(identifiers),
                CognitiveGoalRecord.user_id == source.user_id,
                goal_visibility(PrivacyLevel.L1),
            )
        )
    )
    if allowed != identifiers:
        raise BudgetDenied("delivery_source_private_or_deleted")


def task_delivery_text(title: str, notes: str | None) -> str:
    return f"⏰ 提醒：{title}" + (f"\n{notes}" if notes else "")


def _matches(source: SourceRow | None, fingerprint: str, run_id: UUID) -> bool:
    if source is None:
        return False
    if isinstance(source, TaskItemRecord):
        if source.status not in {"active", "firing"} or source.source == "pnkx":
            return False
        if (source.last_delivery or {}).get("run_id") != str(run_id):
            return False
        text = task_delivery_text(source.title, source.notes)
    else:
        text = source.text
    return hashlib.sha256(text.encode()).hexdigest() == fingerprint


def outcome(row: TaskRunRecord | None) -> DeliveryRunOutcome | None:
    if row is None or row.contract.get("criterion") != "delivery_channels_returned":
        return None
    dispatch = row.contract.get("dispatch_state", "not_started")
    channels = row.contract.get("delivery_channels", [])
    reason = row.contract.get("delivery_reason")
    status = (
        "unknown"
        if dispatch == "unknown"
        else "returned"
        if dispatch == "returned" and channels
        else "not_delivered"
        if dispatch == "returned"
        else "unknown"
        if dispatch == "started" and row.status in {"failed", "cancelled"}
        else "cancelled"
        if row.status == "cancelled"
        else "not_delivered"
        if row.status == "failed"
        else "running"
        if row.status == "running"
        else "not_started"
    )
    return DeliveryRunOutcome(
        run_id=row.id,
        status=status,
        validation_level="V1" if status == "returned" else "V0",
        channels=[str(channel) for channel in channels] if isinstance(channels, list) else [],
        reason_code=str(reason) if reason else None,
    )


async def deliver_once(
    database: Database,
    *,
    table: SourceTable,
    source_id: UUID,
    user_id: UUID,
    text: str,
    entry: str,
    config: RunBudgetConfig,
    dispatch: Callable[[], Awaitable[list[str] | None]],
    run_id: UUID | None = None,
    unavailable_reason: str | None = None,
) -> bool:
    """Return False when another attempt owns this source; never resend unknown work."""
    identifier = run_id or source_id
    parent = current_budget()
    if isinstance(parent, RunModelBudget) and parent.owner_id != user_id:
        raise BudgetDenied("budget_owner_invalid")
    parent_id = parent.run_id if isinstance(parent, RunModelBudget) else None
    now = datetime.now(UTC)
    deadline = now + timedelta(seconds=config.maintenance_deadline_seconds)
    fingerprint = hashlib.sha256(text.encode()).hexdigest()
    try:
        async with database.sessions.begin() as session:
            await assert_current_claim(session)
            owner = await session.get(AppUserRecord, user_id)
            if owner is None or owner.status != "active":
                raise BudgetDenied("budget_owner_invalid")
            source = await _source_lock(session, table, source_id, user_id, pending=True)
            if source is None:
                return False
            if not _matches(source, fingerprint, identifier):
                raise BudgetDenied("delivery_source_changed")
            await _check_goal_sources(session, source)
            created = TaskRunRecord(
                id=identifier,
                user_id=user_id,
                parent_run_id=parent_id,
                request_id=f"{entry}:{identifier}",
                status="accepted",
                privacy_level=source.privacy_level if isinstance(source, TaskItemRecord) else "L1",
                contract={
                    "entry": entry,
                    "source_id": str(source_id),
                    "budget_parent_id": str(parent_id) if parent_id else None,
                    **(
                        {"source_generation": source.fire_count}
                        if isinstance(source, TaskItemRecord)
                        else {}
                    ),
                    "criterion": "delivery_channels_returned",
                    "required_work": [],
                    "dispatch_state": "not_started",
                    "source_content_sha256": fingerprint,
                },
                budget=config.model_dump(mode="json") if parent is None else None,
                deadline=deadline,
                created_at=now,
                updated_at=now,
                state_version=1,
                event_seq=0,
                cancel_epoch=0,
            )
            session.add(created)
            await session.flush()
            await append_run_event(session, created, "run.accepted")
            await transition_run(session, created.id, "running")
    except IntegrityError as error:
        async with database.sessions() as session:
            existing = await session.get(TaskRunRecord, identifier)
            if (
                existing
                and existing.user_id == user_id
                and existing.request_id == f"{entry}:{identifier}"
            ):
                return False
        raise BudgetDenied("delivery_run_identity_conflict") from error
    budget = parent or (
        RunModelBudget(database, run_id=identifier, user_id=user_id, config=config)
        if config.enabled
        else None
    )
    channels: list[str] = []
    reason: str | None = None
    started = False
    permit = None
    port = None
    try:
        with budget_scope(budget):
            if unavailable_reason:
                raise BudgetDenied(unavailable_reason)
            port = current_tool_budget()
            if port is not None:
                permit = await port.reserve_tool(tool_name=entry, user_id=user_id)
            async with database.sessions.begin() as session:
                await assert_current_claim(session)
                source = await _source_lock(session, table, source_id, user_id, pending=True)
                row = await session.get(TaskRunRecord, identifier, with_for_update=True)
                if (
                    row is None
                    or row.status != "running"
                    or row.contract.get("work_cancel_requested")
                ):
                    raise BudgetDenied("budget_run_inactive")
                if not _matches(source, fingerprint, identifier):
                    raise BudgetDenied("delivery_source_changed")
                assert source is not None
                await _check_goal_sources(session, source)
                if utc(row.deadline or deadline) <= datetime.now(UTC):
                    raise BudgetDenied("run_deadline_exceeded")
                row.contract = {**row.contract, "dispatch_state": "started"}
                row.state_version += 1
                row.updated_at = datetime.now(UTC)
                await append_run_event(session, row, "run.delivery.started")
            started = True
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            if permit is not None:
                remaining = min(remaining, permit.remaining_seconds)
            async with asyncio.timeout(max(0, remaining)):
                channels = list(
                    await _dispatch_with_watch(
                        database,
                        table=table,
                        source_id=source_id,
                        user_id=user_id,
                        fingerprint=fingerprint,
                        run_id=identifier,
                        dispatch=dispatch,
                    )
                    or []
                )
            try:
                channels = _CHANNELS.validate_python(channels)
            except ValidationError as error:
                raise BudgetDenied("delivery_channel_receipt_invalid") from error
            reason = None if channels else "no_available_channel"
    except BaseException as error:
        reason = error.reason_code if isinstance(error, BudgetDenied) else type(error).__name__
        if port is not None and permit is not None:
            await port.settle_tool(permit.call_id, reported_ok=None if started else False)
        await _finish(
            database,
            table=table,
            source_id=source_id,
            user_id=user_id,
            run_id=identifier,
            fingerprint=fingerprint,
            channels=[],
            reason=reason,
            state="unknown" if started else "not_started",
            cancelled=isinstance(error, asyncio.CancelledError),
        )
        if not isinstance(error, Exception) or isinstance(error, BudgetDenied):
            raise
        return True
    if port is not None and permit is not None:
        await port.settle_tool(permit.call_id, reported_ok=bool(channels))
    await _finish(
        database,
        table=table,
        source_id=source_id,
        user_id=user_id,
        run_id=identifier,
        fingerprint=fingerprint,
        channels=channels,
        reason=reason,
        state="returned",
    )
    return True


async def _finish(
    database: Database,
    *,
    table: SourceTable,
    source_id: UUID,
    user_id: UUID,
    run_id: UUID,
    fingerprint: str,
    channels: list[str],
    reason: str | None,
    state: str,
    cancelled: bool = False,
) -> None:
    now = datetime.now(UTC)
    async with database.sessions.begin() as session:
        # Source then Run matches admission; cancellation locks only Run.
        source = await _source_lock(session, table, source_id, user_id)
        row = await session.get(TaskRunRecord, run_id, with_for_update=True)
        if row is None or row.user_id != user_id:
            return
        row.contract = {
            **row.contract,
            "dispatch_state": state,
            "delivery_channels": channels,
            "delivery_reason": reason,
        }
        row.state_version += 1
        row.updated_at = now
        await append_run_event(
            session,
            row,
            "run.delivery.returned" if state == "returned" else "run.delivery.stopped",
            payload={"reported_channels": channels, "reason_code": reason},
        )
        if isinstance(source, TaskItemRecord) and (source.last_delivery or {}).get("run_id") == str(
            run_id
        ):
            source.last_delivery = {
                "run_id": str(run_id),
                "fire_count": source.fire_count,
                "trigger_kind": row.contract["entry"],
                "fired_at": utc(source.last_fired_at).isoformat() if source.last_fired_at else None,
                "channels": channels,
                "outcome": state,
                "reason_code": f"deliver_error:{reason}"
                if reason and reason.endswith("Error")
                else reason,
            }
            source.updated_at = now
            cancelled = cancelled or source.status == "cancelled"
            if source.status == "firing":
                source.status, source.completed_at, source.next_fire_at = "done", now, None
        elif (
            source is not None and not isinstance(source, TaskItemRecord) and state != "not_started"
        ):
            # Compatibility: delivered historically means one terminal attempt.
            source.status, source.delivered_at, source.channels, source.updated_at = (
                "delivered",
                now,
                channels,
                now,
            )
        await transition_run(
            session, run_id, "succeeded" if channels else "cancelled" if cancelled else "failed"
        )


def _consume(task: asyncio.Future[Any]) -> None:
    if not task.cancelled():
        task.exception()


async def _dispatch_with_watch(
    database: Database,
    *,
    table: SourceTable,
    source_id: UUID,
    user_id: UUID,
    fingerprint: str,
    run_id: UUID,
    dispatch: Callable[[], Awaitable[list[str] | None]],
) -> list[str] | None:
    async def watch() -> None:
        while True:
            await asyncio.sleep(0.25)
            async with database.sessions() as session:
                await assert_current_claim(session)
                row = await session.get(TaskRunRecord, run_id)
                source = cast(SourceRow | None, await session.get(table, source_id))
                owner = await session.get(AppUserRecord, user_id)
                if (
                    row is None
                    or row.user_id != user_id
                    or row.status != "running"
                    or row.contract.get("work_cancel_requested")
                    or owner is None
                    or owner.status != "active"
                ):
                    raise BudgetDenied("budget_run_inactive")
                if (
                    source is None
                    or source.user_id != user_id
                    or not _matches(source, fingerprint, run_id)
                ):
                    raise BudgetDenied("delivery_source_changed")
                await _check_goal_sources(session, source)
                parent_id = row.parent_run_id or row.contract.get("budget_parent_id")
                if parent_id is not None:
                    parent = await session.get(TaskRunRecord, UUID(str(parent_id)))
                    if (
                        parent is None
                        or parent.user_id != user_id
                        or parent.status in {"failed", "cancelled"}
                        or parent.contract.get("work_cancel_requested")
                    ):
                        raise BudgetDenied("budget_run_inactive")

    async def invoke() -> list[str] | None:
        return await dispatch()

    operation, watcher = asyncio.create_task(invoke()), asyncio.create_task(watch())
    try:
        done, _ = await asyncio.wait({operation, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            await watcher
        return await operation
    finally:
        for task in (operation, watcher):
            if not task.done():
                task.cancel()
        # A custom transport that suppresses cancellation cannot be forcefully
        # stopped; retain unknown and never wait indefinitely or dispatch again.
        await asyncio.wait({operation, watcher}, timeout=0.25)
        for task in (operation, watcher):
            task.add_done_callback(_consume)


async def recover_expired_deliveries(database: Database) -> int:
    now = datetime.now(UTC)
    recovered = 0
    after: UUID | None = None
    while True:
        query = (
            select(TaskRunRecord.id)
            .where(
                TaskRunRecord.status.in_({"accepted", "running"}),
                TaskRunRecord.deadline <= now,
                TaskRunRecord.contract["criterion"].as_string() == "delivery_channels_returned",
            )
            .order_by(TaskRunRecord.id)
            .limit(100)
        )
        if after is not None:
            query = query.where(TaskRunRecord.id > after)
        async with database.sessions() as session:
            identifiers = list(await session.scalars(query))
        if not identifiers:
            return recovered
        for identifier in identifiers:
            async with database.sessions.begin() as session:
                row = await session.get(TaskRunRecord, identifier, with_for_update=True)
                if (
                    row is None
                    or row.status not in {"accepted", "running"}
                    or utc(row.deadline or now) > now
                ):
                    continue
                row.contract = {
                    **row.contract,
                    "dispatch_state": "unknown"
                    if row.contract.get("dispatch_state") == "started"
                    else "not_started",
                    "delivery_reason": "delivery_interrupted",
                }
                calls = list(
                    await session.scalars(
                        select(ModelReservationRecord.call_id).where(
                            ModelReservationRecord.run_id == row.id,
                            ModelReservationRecord.state == "reserved",
                        )
                    )
                )
                await session.execute(
                    update(ModelReservationRecord)
                    .where(
                        ModelReservationRecord.run_id == row.id,
                        ModelReservationRecord.state == "reserved",
                    )
                    .values(state="unknown", settled_at=now)
                )
                await transition_run(session, row.id, "failed")
                recovered += 1
            # Settlement locks Cost before Run. Release Run first to avoid a
            # recovery/settlement cycle; unknown holds remain throughout.
            if calls:
                async with database.sessions.begin() as session:
                    await session.execute(
                        update(ModelCostRecord)
                        .where(
                            ModelCostRecord.call_id.in_(calls),
                            ModelCostRecord.state == "reserved",
                        )
                        .values(state="unknown", settled_at=now)
                    )
        after = identifiers[-1]
