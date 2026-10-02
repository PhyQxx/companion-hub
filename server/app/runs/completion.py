"""Durable model-only runs for owned entry points outside conversation workers."""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.config import ConfigSnapshot
from app.db import (
    AppUserRecord,
    ConversationRecord,
    Database,
    ModelReservationRecord,
    TaskRunRecord,
)
from app.harness.budget import BudgetDenied, budget_scope, current_budget
from app.ids import uuid7
from app.llm.contracts import CompletionRequest, CompletionResult
from app.schemas import PrivacyLevel

from .budget import RunModelBudget, utc
from .store import append_run_event, transition_run

_MODEL_OWNER: ContextVar[UUID | None] = ContextVar("model_run_owner", default=None)


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
) -> CompletionResult:
    if request.privacy_level == PrivacyLevel.L3:
        raise BudgetDenied("ephemeral_model_run_forbidden")
    inherited = current_budget()
    if isinstance(inherited, RunModelBudget) and inherited.owner_id != user_id:
        raise BudgetDenied("budget_owner_invalid")
    if inherited is not None:
        # Workers already own a durable run and its cancellation/budget fence.
        return await complete(request)
    await recover_expired_model_runs(database)
    now = datetime.now(UTC)
    deadline = now + timedelta(seconds=snapshot.config.run_budget.maintenance_deadline_seconds)
    if expires_at is not None:
        deadline = min(deadline, utc(expires_at))
    if deadline <= now:
        raise BudgetDenied("run_deadline_exceeded")
    run_id = uuid7()
    try:
        async with database.sessions.begin() as session:
            if conversation_id is not None:
                conversation = await session.scalar(
                    select(ConversationRecord)
                    .where(
                        ConversationRecord.id == conversation_id,
                        ConversationRecord.user_id == user_id,
                    )
                    .with_for_update()
                )
                if conversation is None:
                    raise BudgetDenied("run_source_not_found")
            owner = await session.get(AppUserRecord, user_id)
            if owner is None or owner.status != "active":
                raise BudgetDenied("budget_owner_invalid")
            row = TaskRunRecord(
                id=run_id,
                user_id=user_id,
                conversation_id=conversation_id,
                request_id=f"{kind}:{source_id}",
                status="accepted",
                privacy_level=str(request.privacy_level),
                config_version=snapshot.version,
                contract={
                    "kind": kind,
                    "source_id": str(source_id),
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
    except IntegrityError as error:
        # The same owned source must not silently acquire a fresh quota when
        # delivered again. Result caching/semantic retries need their own contract.
        raise BudgetDenied("model_run_source_already_processed") from error
    budget = (
        RunModelBudget(database, run_id=run_id, user_id=user_id, config=snapshot.config.run_budget)
        if snapshot.config.run_budget.enabled
        else None
    )
    try:
        with budget_scope(budget):
            async with asyncio.timeout(max(0, (deadline - datetime.now(UTC)).total_seconds())):
                result = await complete(request)
        async with database.sessions.begin() as session:
            current = await session.get(TaskRunRecord, run_id, with_for_update=True)
            if current is None or current.status != "running":
                raise BudgetDenied("budget_run_inactive")
            await transition_run(session, run_id, "succeeded")
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
