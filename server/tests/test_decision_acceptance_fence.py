"""Decision acceptance locks the account and snapshots nested source evidence."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_cognitive_save_guard import count, setup
from test_cognitive_save_guard import database as save_database
from test_job_lifecycle_fence import during_commit
from test_memory import candidate

from app.cognition import (
    AttentionEngine,
    CognitiveDecision,
    CognitiveStore,
    GoalKind,
    RuleBasedDeliberator,
    SemanticEvent,
    WorldState,
    WorldStateBuilder,
)
from app.db import (
    AppUserRecord,
    CognitiveDecisionRecord,
    CognitiveGoalRecord,
    Database,
    MemoryRecord,
)
from app.harness.budget import BudgetDenied
from app.memory import MemoryRetriever, MemoryStore

database = save_database


async def pending(
    database: Database,
) -> tuple[CognitiveStore, SemanticEvent, WorldState, CognitiveDecision]:
    store, source = await setup(database, conversation=False)
    state = await WorldStateBuilder(database, store).build(source)
    decision = await RuleBasedDeliberator().deliberate(
        source, state, AttentionEngine().evaluate(source, state)
    )
    return store, source, state, decision


async def grounded(
    database: Database, kind: str
) -> tuple[CognitiveStore, SemanticEvent, WorldState, CognitiveDecision, int | UUID]:
    store, source = await setup(database, conversation=False)
    source = source.model_copy(update={"summary": "fixture cilantro preference"})
    memory = MemoryStore(database)
    if kind == "goal":
        goal = await store.create_goal(
            user_id=source.user_id,
            kind=GoalKind.USER,
            title="fixture goal",
            source_kind="manual",
            source_id="fixture",
        )
        identifier: int | UUID = goal.id
    else:
        entry = await memory.add(candidate("fixture cilantro preference"), user_id=source.user_id)
        identifier = entry.id
    state = await WorldStateBuilder(
        database, store, memory_retriever=MemoryRetriever(memory)
    ).build(source)
    assert state.active_goals if kind == "goal" else state.memory_evidence_ids
    decision = await RuleBasedDeliberator().deliberate(
        source, state, AttentionEngine().evaluate(source, state)
    )
    return store, source, state, decision, identifier


@pytest.mark.parametrize("kind", ["goal", "memory"])
async def test_waiting_acceptance_rechecks_committed_evidence_changes(
    database: Database, kind: str
) -> None:
    store, source, state, decision, identifier = await grounded(database, kind)

    async def write(session: AsyncSession) -> None:
        if kind == "goal":
            await session.execute(
                update(CognitiveGoalRecord)
                .where(CognitiveGoalRecord.id == identifier)
                .values(status="cancelled")
            )
        else:
            await session.execute(
                update(MemoryRecord)
                .where(MemoryRecord.id == identifier)
                .values(updated_at=datetime.now(UTC) + timedelta(minutes=1))
            )

    async def follow() -> None:
        with pytest.raises(BudgetDenied, match="model_source_changed"):
            await store.save_decision(decision, event=source, state=state)

    await during_commit(database, write, follow)
    assert await count(database) == 0


@pytest.mark.parametrize("kind", ["goal", "memory"])
async def test_evidence_mutation_waits_until_accepted_decision_commits(
    database: Database, kind: str
) -> None:
    store, source, state, decision, identifier = await grounded(database, kind)
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.validate_world_snapshot

    async def validate(
        session: AsyncSession, event: SemanticEvent, state: WorldState, *, lock: bool = False
    ) -> None:
        await original(session, event, state, lock=lock)
        entered.set()
        await release.wait()

    store.validate_world_snapshot = validate  # type: ignore[method-assign]
    task = asyncio.create_task(store.save_decision(decision, event=source, state=state))
    mutation: asyncio.Task[None] | None = None

    async def mutate() -> None:
        async with database.sessions.begin() as session:
            if kind == "goal":
                await session.execute(
                    update(CognitiveGoalRecord)
                    .where(CognitiveGoalRecord.id == identifier)
                    .values(status="cancelled")
                )
            else:
                await session.execute(
                    update(MemoryRecord)
                    .where(MemoryRecord.id == identifier)
                    .values(updated_at=datetime.now(UTC) + timedelta(minutes=1))
                )

    try:
        await asyncio.wait_for(entered.wait(), 3)
        mutation = asyncio.create_task(mutate())
        await asyncio.sleep(0.1)
        assert not mutation.done(), "evidence changed between validation and accepted commit"
        release.set()
        await asyncio.wait_for(task, 3)
        await asyncio.wait_for(mutation, 3)
        assert await count(database) == 1
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if mutation is not None:
            if not mutation.done():
                mutation.cancel()
            await asyncio.gather(mutation, return_exceptions=True)


async def test_waiting_decision_rejects_account_revoked_before_lock(database: Database) -> None:
    store, source, state, decision = await pending(database)

    async def follow() -> None:
        with pytest.raises(BudgetDenied, match="budget_owner_invalid"):
            await store.save_decision(decision, event=source, state=state)

    await during_commit(
        database,
        lambda session: session.execute(
            update(AppUserRecord)
            .where(AppUserRecord.id == source.user_id)
            .values(status="disabled")
        ),
        follow,
    )
    assert await count(database) == 0


async def test_account_revocation_waits_for_decision_acceptance(database: Database) -> None:
    store, source, state, decision = await pending(database)
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.validate_world_snapshot

    async def validate(
        session: AsyncSession, event: SemanticEvent, state: WorldState, *, lock: bool = False
    ) -> None:
        entered.set()
        await release.wait()
        await original(session, event, state, lock=lock)

    # Pause between the owner guard and durable decision insertion.
    store.validate_world_snapshot = validate  # type: ignore[method-assign]
    accepted = asyncio.create_task(store.save_decision(decision, event=source, state=state))
    revoked: asyncio.Task[None] | None = None

    async def revoke(owner: UUID) -> None:
        async with database.sessions.begin() as session:
            await session.execute(
                update(AppUserRecord).where(AppUserRecord.id == owner).values(status="disabled")
            )

    try:
        await asyncio.wait_for(entered.wait(), 3)
        revoked = asyncio.create_task(revoke(source.user_id))
        await asyncio.sleep(0.1)
        assert not revoked.done(), "revocation passed the acceptance guard before its commit"
        release.set()
        await asyncio.wait_for(accepted, 3)
        await asyncio.wait_for(revoked, 3)
        assert await count(database) == 1
    finally:
        release.set()
        if not accepted.done():
            accepted.cancel()
        await asyncio.gather(accepted, return_exceptions=True)
        if revoked is not None:
            if not revoked.done():
                revoked.cancel()
            await asyncio.gather(revoked, return_exceptions=True)


@pytest.mark.parametrize("change", ["decision", "world"])
async def test_nested_caller_mutation_cannot_change_accepted_snapshot(
    database: Database, change: str
) -> None:
    store, source, state, decision = await pending(database)
    original_evidence = tuple(source.evidence_ids)
    original_reasons = tuple(decision.reason_codes)
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.validate_world_snapshot

    async def validate(
        session: AsyncSession, event: SemanticEvent, state: WorldState, *, lock: bool = False
    ) -> None:
        entered.set()
        await release.wait()
        await original(session, event, state, lock=lock)

    store.validate_world_snapshot = validate  # type: ignore[method-assign]
    task = asyncio.create_task(store.save_decision(decision, event=source, state=state))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if change == "decision":
            source.evidence_ids.append("late source")
            decision.evidence_ids.append("late decision")
            decision.reason_codes.append("late reason")
        else:
            state.memory_evidence_ids.append("late unvalidated source")
        release.set()
        await asyncio.wait_for(task, 3)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    async with database.sessions() as session:
        row = await session.scalar(
            select(CognitiveDecisionRecord).where(CognitiveDecisionRecord.id == decision.id)
        )
        assert row is not None
        assert tuple(row.evidence_ids) == original_evidence
        assert tuple(row.reason_codes) == original_reasons
