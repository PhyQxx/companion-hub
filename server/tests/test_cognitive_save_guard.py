"""Evidence can be revoked after deliberation but before durable decision acceptance."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from test_memory import candidate

from app.cognition import (
    AttentionEngine,
    AttentionResult,
    CognitiveCycle,
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
    Base,
    CognitiveDecisionRecord,
    CognitiveGoalRecord,
    ConversationRecord,
    Database,
    DeletionLedgerRecord,
    MemoryRecord,
    TimelineEventRecord,
    create_database,
)
from app.harness.budget import BudgetDenied
from app.ids import uuid7
from app.memory import MemoryRetriever, MemoryStore
from app.schemas import PrivacyLevel
from app.timeline import TimelineActor, TimelineSourceType, TimelineStore


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    value = create_database(f"sqlite+aiosqlite:///{tmp_path / 'save.db'}")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield value
    finally:
        await value.close()


async def setup(
    database: Database, *, conversation: bool = True
) -> tuple[CognitiveStore, SemanticEvent]:
    owner, conversation_id = uuid7(), uuid7() if conversation else None
    now = datetime.now(UTC)
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=owner, display_name="Synthetic owner", status="active"))
        await session.flush()
        if conversation_id:
            session.add(
                ConversationRecord(
                    id=conversation_id, user_id=owner, status="active", created_at=now
                )
            )
    return CognitiveStore(database), SemanticEvent(
        event_id=uuid7(),
        user_id=owner,
        conversation_id=conversation_id,
        kind="user_arrived_home",
        summary="Synthetic event",
        occurred_at=now,
        expires_at=now + timedelta(minutes=5),
        privacy_level=PrivacyLevel.L1,
        confidence=1,
        evidence_ids=["synthetic"],
    )


async def count(database: Database) -> int:
    async with database.sessions() as session:
        return int(await session.scalar(select(func.count(CognitiveDecisionRecord.id))) or 0)


@pytest.mark.parametrize("change", ["conversation", "intent", "owner", "goal"])
async def test_save_revalidates_after_deliberator_returns(database: Database, change: str) -> None:
    store, event = await setup(database)
    goal = await store.create_goal(
        user_id=event.user_id,
        kind=GoalKind.USER,
        source_kind="manual",
        source_id="synthetic",
        title="Synthetic goal",
    )

    class Changed:
        async def deliberate(
            self,
            actual: SemanticEvent,
            state: WorldState,
            attention: AttentionResult,
        ) -> CognitiveDecision:
            result = await RuleBasedDeliberator().deliberate(actual, state, attention)
            async with database.sessions.begin() as session:
                if change == "conversation":
                    row = await session.get(ConversationRecord, event.conversation_id)
                    assert row is not None
                    await session.delete(row)
                elif change == "intent":
                    assert event.conversation_id is not None
                    session.add(
                        DeletionLedgerRecord(
                            entity_kind="message",
                            entity_id=event.conversation_id.hex.upper(),
                            deleted_ids=[],
                            requested_by=str(event.user_id),
                        )
                    )
                elif change == "owner":
                    await session.execute(
                        update(AppUserRecord)
                        .where(AppUserRecord.id == event.user_id)
                        .values(status="disabled")
                    )
                else:
                    await session.execute(
                        update(CognitiveGoalRecord)
                        .where(CognitiveGoalRecord.id == goal.id)
                        .values(title="Changed")
                    )
            return result

    cycle = CognitiveCycle(
        store, WorldStateBuilder(database, store), AttentionEngine(threshold=0), Changed()
    )
    with pytest.raises(BudgetDenied):
        await cycle.evaluate(event)
    assert await count(database) == 0


@pytest.mark.parametrize(
    "field",
    ["user_id", "event_id", "conversation_id", "trigger_kind", "evidence_ids", "expires_at"],
)
async def test_deliberator_cannot_replace_source_contract(database: Database, field: str) -> None:
    store, event = await setup(database)

    class Forged:
        async def deliberate(
            self,
            actual: SemanticEvent,
            state: WorldState,
            attention: AttentionResult,
        ) -> CognitiveDecision:
            result = await RuleBasedDeliberator().deliberate(actual, state, attention)
            value: object = (
                ["invented"]
                if field == "evidence_ids"
                else "invented"
                if field == "trigger_kind"
                else None
                if field == "expires_at"
                else uuid7()
            )
            return result.model_copy(update={field: value})

    cycle = CognitiveCycle(
        store, WorldStateBuilder(database, store), AttentionEngine(threshold=0), Forged()
    )
    with pytest.raises(BudgetDenied, match="decision_source_mismatch"):
        await cycle.evaluate(event)
    assert await count(database) == 0


async def test_expiry_at_save_boundary_discards_decision(database: Database) -> None:
    store, event = await setup(database)

    class Delayed:
        async def deliberate(
            self, actual: SemanticEvent, state: WorldState, attention: AttentionResult
        ) -> CognitiveDecision:
            result = await RuleBasedDeliberator().deliberate(actual, state, attention)
            # Set an already-expired event before evaluation, but run a fixed stub
            # to test the write boundary without sleeping or a real provider.
            return result

    expired = event.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    cycle = CognitiveCycle(
        store, WorldStateBuilder(database, store), AttentionEngine(threshold=0), Delayed()
    )
    with pytest.raises(BudgetDenied, match="decision_expired"):
        await cycle.evaluate(expired)
    assert await count(database) == 0


async def test_suppression_and_l3_keep_their_recording_boundary(database: Database) -> None:
    store, event = await setup(database)
    cycle = CognitiveCycle(
        store, WorldStateBuilder(database, store), AttentionEngine(), RuleBasedDeliberator()
    )
    expired = event.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    await cycle.suppress(expired, "event_expired")
    assert await count(database) == 1
    private = event.model_copy(update={"privacy_level": PrivacyLevel.L3})
    decision = await cycle.suppress(private, "l3_no_persistence")
    assert await count(database) == 1
    with pytest.raises(BudgetDenied, match="decision_privacy_denied"):
        await store.save_decision(decision, event=private)


@pytest.mark.parametrize("kind", ["memory", "timeline"])
async def test_context_revoked_after_deliberation_is_not_saved(
    database: Database, kind: str
) -> None:
    store, event = await setup(database)
    event = event.model_copy(update={"summary": "fixture cilantro preference"})
    memory, timeline = MemoryStore(database), TimelineStore(database)
    if kind == "memory":
        entry = await memory.add(candidate("fixture cilantro preference"), user_id=event.user_id)
        identifier = entry.id
    else:
        indexed = await timeline.index_custom(
            user_id=event.user_id,
            source_id="fixture",
            source_type=TimelineSourceType.SYSTEM,
            actor=TimelineActor.SYSTEM,
            event_type="fixture",
            title="fixture",
            summary="fixture cilantro preference",
            privacy_level=PrivacyLevel.L1,
            occurred_at=datetime.now(UTC),
        )
        assert indexed is not None
        identifier = indexed.id

    class Revoked:
        async def deliberate(
            self, actual: SemanticEvent, state: WorldState, attention: AttentionResult
        ) -> CognitiveDecision:
            assert len(state.context_references) == 1
            result = await RuleBasedDeliberator().deliberate(actual, state, attention)
            async with database.sessions.begin() as session:
                row = await session.get(
                    MemoryRecord if kind == "memory" else TimelineEventRecord, identifier
                )
                assert isinstance(row, (MemoryRecord, TimelineEventRecord))
                row.privacy_level = "L2"
            return result

    cycle = CognitiveCycle(
        store,
        WorldStateBuilder(
            database, store, memory_retriever=MemoryRetriever(memory), timeline_store=timeline
        ),
        AttentionEngine(threshold=0),
        Revoked(),
    )
    with pytest.raises(BudgetDenied, match="model_source_changed"):
        await cycle.evaluate(event)
    assert await count(database) == 0
