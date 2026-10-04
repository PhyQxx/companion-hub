"""Durable model-only runs for owned entry points outside conversation workers."""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ConfigSnapshot
from app.db import (
    AppUserRecord,
    ConversationRecord,
    Database,
    ModelReservationRecord,
    TaskRunRecord,
)
from app.db.claims import assert_current_claim
from app.harness.budget import BudgetDenied, budget_scope, current_budget, tool_budget_scope
from app.harness.guarded_call import guarded_call
from app.harness.run_trace import DisabledRunTrace, current_run_trace, run_trace_scope
from app.ids import uuid7
from app.llm.contracts import CompletionRequest, CompletionResult
from app.schemas import PrivacyLevel

from .budget import RunModelBudget, utc
from .store import append_run_event, transition_run
from .trace_sources import check_trace_binding, extend_run_trace, require_run_trace, trace_source

_MODEL_OWNER: ContextVar[UUID | None] = ContextVar("model_run_owner", default=None)


def current_model_owner() -> UUID | None:
    return _MODEL_OWNER.get()


@contextmanager
def model_owner(user_id: UUID) -> Iterator[None]:
    """Carry an already authorized owner across legacy analyzer ports."""
    token = _MODEL_OWNER.set(user_id)
    try:
        yield
    finally:
        _MODEL_OWNER.reset(token)


async def complete_owned_with_run(
    database: Database,
    snapshot: ConfigSnapshot,
    request: CompletionRequest,
    complete: Callable[[CompletionRequest], Awaitable[CompletionResult]],
    *,
    kind: str,
) -> CompletionResult:
    owner = _MODEL_OWNER.get()
    if owner is None:
        raise BudgetDenied("model_run_owner_missing")
    return await complete_with_run(
        database,
        snapshot,
        request,
        complete,
        user_id=owner,
        kind=kind,
        source_id=request.trace_id,
    )


async def complete_with_run(
    database: Database,
    snapshot: ConfigSnapshot,
    request: CompletionRequest,
    complete: Callable[[CompletionRequest], Awaitable[CompletionResult]],
    *,
    user_id: UUID,
    kind: str,
    source_id: UUID,
    conversation_id: UUID | None = None,
    expires_at: datetime | None = None,
    source_guard: Callable[[], Awaitable[None]] | None = None,
) -> CompletionResult:
    if request.privacy_level == PrivacyLevel.L3:
        raise BudgetDenied("ephemeral_model_run_forbidden")
    trace = current_run_trace()
    if trace is not None:
        check_trace_binding(trace, database, user_id)
        if len(trace.sources) >= 32:
            raise BudgetDenied("run_trace_changed")

    async def validate(run_id: UUID | None) -> None:
        async with database.sessions() as session:
            await assert_current_claim(session)
            owner = await session.get(AppUserRecord, user_id)
            if owner is None or owner.status != "active":
                raise BudgetDenied("budget_owner_invalid")
            if trace is not None:
                await require_run_trace(
                    session, trace, user_id=user_id, privacy_level=request.privacy_level
                )
            if conversation_id is not None:
                conversation = await session.get(ConversationRecord, conversation_id)
                if conversation is None or conversation.user_id != user_id:
                    raise BudgetDenied("run_source_not_found")
            if run_id is not None:
                run = await session.get(TaskRunRecord, run_id)
                if (
                    run is None
                    or run.user_id != user_id
                    or run.status in {"failed", "cancelled"}
                    or run.contract.get("work_cancel_requested")
                ):
                    raise BudgetDenied("budget_run_inactive")
                if run.deadline and utc(run.deadline) <= datetime.now(UTC):
                    raise BudgetDenied("run_deadline_exceeded")
        if expires_at is not None and utc(expires_at) <= datetime.now(UTC):
            raise BudgetDenied("run_deadline_exceeded")
        if source_guard is not None:
            try:
                await source_guard()
            except BudgetDenied:
                raise
            except Exception as error:
                raise BudgetDenied("model_source_check_failed") from error

    inherited = None if trace else current_budget()
    if isinstance(inherited, RunModelBudget) and inherited.owner_id != user_id:
        raise BudgetDenied("budget_owner_invalid")
    if inherited is not None:
        # Workers already own a durable run and its cancellation/budget fence.
        return await guarded_call(
            lambda: complete(request),
            lambda: validate(inherited.run_id if isinstance(inherited, RunModelBudget) else None),
        )
    await validate(None)
    await recover_expired_model_runs(database)
    now = datetime.now(UTC)
    deadline = now + timedelta(seconds=snapshot.config.run_budget.maintenance_deadline_seconds)
    if expires_at is not None:
        deadline = min(deadline, utc(expires_at))
    if trace and trace.expires_at is not None:
        deadline = min(deadline, utc(trace.expires_at))
    if deadline <= now:
        raise BudgetDenied("run_deadline_exceeded")
    run_id = uuid7()

    async def lock_context(session: AsyncSession) -> None:
        if conversation_id is not None:
            # Preserve conversation -> claim -> owner order with a real
            # first-write source fence on both SQLite and PostgreSQL.
            conversation = await session.scalar(
                update(ConversationRecord)
                .where(
                    ConversationRecord.id == conversation_id,
                    ConversationRecord.user_id == user_id,
                )
                .values(id=ConversationRecord.id)
                .returning(ConversationRecord.id)
            )
            if conversation is None:
                raise BudgetDenied("run_source_not_found")
        await assert_current_claim(session)
        owner = await session.scalar(
            update(AppUserRecord)
            .where(AppUserRecord.id == user_id, AppUserRecord.status == "active")
            .values(id=AppUserRecord.id)
            .returning(AppUserRecord.id)
        )
        if owner is None:
            raise BudgetDenied("budget_owner_invalid")
        if trace is not None:
            await require_run_trace(
                session, trace, user_id=user_id, privacy_level=request.privacy_level, lock=True
            )
        if deadline <= datetime.now(UTC):
            raise BudgetDenied("run_deadline_exceeded")

    try:
        async with database.sessions.begin() as session:
            await lock_context(session)
            row = TaskRunRecord(
                id=run_id,
                user_id=user_id,
                parent_run_id=trace.run_id if trace else None,
                conversation_id=conversation_id,
                request_id=f"{kind}:{source_id}",
                status="accepted",
                privacy_level=str(request.privacy_level),
                config_version=snapshot.version,
                contract={
                    "kind": kind,
                    "source_id": str(source_id),
                    "request_trace_id": str(request.trace_id),
                    "criterion": "model_result_returned",
                    "required_work": [],
                },
                budget=snapshot.config.run_budget.model_dump(mode="json"),
                deadline=deadline,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            await session.flush()
            await append_run_event(session, row, "run.accepted")
            await transition_run(session, run_id, "running")
            await session.flush()
            if trace is not None:
                await require_run_trace(
                    session, trace, user_id=user_id, privacy_level=request.privacy_level
                )
            if deadline <= datetime.now(UTC):
                raise BudgetDenied("run_deadline_exceeded")
    except IntegrityError as error:
        # The same owned source must not silently acquire a fresh quota when
        # delivered again. Result caching/semantic retries need their own contract.
        raise BudgetDenied("model_run_source_already_processed") from error
    budget = (
        RunModelBudget(database, run_id=run_id, user_id=user_id, config=snapshot.config.run_budget)
        if snapshot.config.run_budget.enabled and trace is None
        else None
    )
    try:
        with (
            budget_scope(budget),
            tool_budget_scope(None),
            run_trace_scope(
                extend_run_trace(trace, row)
                if trace
                else DisabledRunTrace(
                    id(database), user_id, (trace_source(row, maintenance=False),)
                )
                if not snapshot.config.run_budget.enabled
                else None
            ),
        ):
            async with asyncio.timeout(max(0, (deadline - datetime.now(UTC)).total_seconds())):
                result = await guarded_call(lambda: complete(request), lambda: validate(run_id))
        async with database.sessions.begin() as session:
            await lock_context(session)
            current = await session.scalar(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == run_id, TaskRunRecord.user_id == user_id)
                .values(updated_at=TaskRunRecord.updated_at)
                .returning(TaskRunRecord)
                .execution_options(synchronize_session=False, populate_existing=True)
            )
            if (
                current is None
                or current.status != "running"
                or current.contract.get("work_cancel_requested")
            ):
                raise BudgetDenied("budget_run_inactive")
            if current.deadline and utc(current.deadline) <= datetime.now(UTC):
                raise BudgetDenied("run_deadline_exceeded")
            await transition_run(session, run_id, "succeeded")
            await session.flush()
            if trace is not None:
                await require_run_trace(
                    session, trace, user_id=user_id, privacy_level=request.privacy_level
                )
            if min(deadline, utc(current.deadline or deadline)) <= datetime.now(UTC):
                raise BudgetDenied("run_deadline_exceeded")
        return result
    except BaseException as error:
        async with database.sessions.begin() as session:
            await transition_run(
                session,
                run_id,
                "cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
            )
        raise


async def recover_expired_model_runs(database: Database) -> int:
    """Expire interrupted calls only after their absolute deadline, across workers.

    Prompts/results are not persisted, so these runs cannot be replayed. Unknown
    usage keeps its reservation; live calls on another worker are not cancelled.
    """
    now = datetime.now(UTC)
    count = 0
    async with database.sessions.begin() as session:
        rows = await session.scalars(
            select(TaskRunRecord)
            .where(
                TaskRunRecord.status.in_({"accepted", "running"}),
                TaskRunRecord.deadline <= now,
                TaskRunRecord.contract["criterion"].as_string() == "model_result_returned",
            )
            .order_by(TaskRunRecord.id)
            .with_for_update()
        )
        for row in rows:
            if row.contract.get("criterion") != "model_result_returned":
                continue
            await transition_run(session, row.id, "failed")
            await session.execute(
                update(ModelReservationRecord)
                .where(
                    ModelReservationRecord.run_id == row.id,
                    ModelReservationRecord.state == "reserved",
                )
                .values(state="unknown", settled_at=now)
            )
            count += 1
    return count
