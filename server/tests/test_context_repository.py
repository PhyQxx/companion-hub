import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from test_database_config import config_yaml
from test_goal_delivery_runs import database as goal_database
from test_goal_delivery_runs import seed
from test_memory import candidate

from app.cognition import AttentionResult, RouterDeliberator, SemanticEvent, WorldStateBuilder
from app.config import ConfigStore
from app.context.repository import ContextSourceInvalidated, validate_references, version_stamp
from app.db import (
    ConversationRecord,
    Database,
    DeletionLedgerRecord,
    MemoryRecord,
    MessageRecord,
    TaskRunRecord,
    TimelineEventRecord,
)
from app.harness.budget import BudgetDenied
from app.harness.context import ContextReference
from app.ids import uuid7
from app.llm import CompletionRequest, CompletionResult
from app.memory import (
    MemoryCandidate,
    MemoryRetriever,
    MemorySourceKind,
    MemorySourceRef,
    MemoryStore,
    MemoryType,
)
from app.schemas import PrivacyLevel
from app.timeline import TimelineActor, TimelineSourceType, TimelineStore

database = goal_database


@pytest.mark.parametrize("kind", ["memory", "timeline"])
@pytest.mark.parametrize("timing", ["before", "inflight", "returned"])
async def test_world_evidence_revocation_blocks_or_discards_model(
    database: Database,
    tmp_path: Path,
    kind: str,
    timing: str,
) -> None:
    owner, store, _ = await seed(database)
    memory, timeline = MemoryStore(database), TimelineStore(database)
    if kind == "memory":
        entry = await memory.add(candidate("fixture cilantro preference"), user_id=owner)
        identifier = entry.id
    else:
        indexed = await timeline.index_custom(
            user_id=owner,
            source_id="fixture",
            source_type=TimelineSourceType.SYSTEM,
            actor=TimelineActor.SYSTEM,
            event_type="fixture",
            title="fixture",
            summary="fixture cilantro preference",
            privacy_level=PrivacyLevel.L1,
            occurred_at=datetime.now(UTC),
        )
        assert indexed
        identifier = indexed.id
    event = SemanticEvent(
        event_id=uuid7(),
        user_id=owner,
        kind="fixture",
        summary="fixture cilantro preference",
        privacy_level=PrivacyLevel.L1,
        occurred_at=datetime.now(UTC),
        evidence_ids=["fixture"],
    )
    state = await WorldStateBuilder(
        database, store, memory_retriever=MemoryRetriever(memory), timeline_store=timeline
    ).build(event, now=datetime.now(UTC) + timedelta(seconds=1))
    assert len(state.context_references) == 1
    assert "context_references" not in state.model_dump(mode="json")
    assert "cilantro" not in str(state.context_references[0].manifest())
    started, stopped = asyncio.Event(), asyncio.Event()
    calls = 0

    async def revoke() -> None:
        async with database.sessions.begin() as session:
            row = await session.get(
                MemoryRecord if kind == "memory" else TimelineEventRecord, identifier
            )
            assert isinstance(row, (MemoryRecord, TimelineEventRecord))
            row.privacy_level = "L2"

    class Backend:
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            nonlocal calls
            calls += 1
            started.set()
            try:
                if timing == "inflight":
                    await asyncio.sleep(30)
                else:
                    await revoke()
                return CompletionResult(
                    text='{"decision":"inform","message":"obsolete"}',
                    provider="fixture",
                    model="fixture",
                    endpoint="fixture",
                    route=request.route,
                    latency_ms=0,
                )
            finally:
                stopped.set()

    path = tmp_path / "config.yaml"
    path.write_text(config_yaml())
    config = ConfigStore(path)
    await config.load()
    deliberator = RouterDeliberator(config, database=database, router_builder=lambda _: Backend())
    if timing == "before":
        await revoke()
    running = asyncio.create_task(
        deliberator.deliberate(
            event,
            state,
            AttentionResult(
                score=1, threshold=0.5, reason_codes=["fixture"], should_deliberate=True
            ),
        )
    )
    if timing == "inflight":
        await asyncio.wait_for(started.wait(), 3)
        await revoke()
    with pytest.raises(BudgetDenied, match="model_source_changed"):
        await asyncio.wait_for(running, 3)
    assert calls == (0 if timing == "before" else 1)
    if calls:
        assert stopped.is_set()
    async with database.sessions() as session:
        rows = list(await session.scalars(select(TaskRunRecord)))
        assert len(rows) == calls
        assert all(row.status == "failed" for row in rows)


@pytest.mark.parametrize("change", ["conversation_intent", "message_intent", "private", "missing"])
async def test_memory_ancestry_checks_source_and_unreplayed_deletions(
    database: Database, change: str
) -> None:
    owner, _, _ = await seed(database)
    conversation, message = uuid7(), uuid7()
    async with database.sessions.begin() as session:
        session.add(
            ConversationRecord(
                id=conversation,
                user_id=owner,
                status="active",
                created_at=datetime.now(UTC),
                last_active_at=datetime.now(UTC),
            )
        )
        session.add(
            MessageRecord(
                id=message,
                conversation_id=conversation,
                turn_id=uuid7(),
                seq=1,
                role="user",
                content="fixture source",
                privacy_level="L1",
                created_at=datetime.now(UTC),
            )
        )
    memory = MemoryStore(database)
    parent = await memory.add(
        MemoryCandidate(
            type=MemoryType.SEMANTIC,
            content="fixture parent",
            privacy_level=PrivacyLevel.L1,
            sources=[MemorySourceRef(source_kind=MemorySourceKind.MESSAGE, source_id=str(message))],
        ),
        user_id=owner,
    )
    child = await memory.add(
        MemoryCandidate(
            type=MemoryType.SEMANTIC,
            content="fixture child",
            privacy_level=PrivacyLevel.L1,
            sources=[
                MemorySourceRef(source_kind=MemorySourceKind.MEMORY, source_id=str(parent.id))
            ],
        ),
        user_id=owner,
    )
    ref = ContextReference(
        kind="memory",
        source_id=str(child.id),
        owner_id=str(owner),
        privacy_level="L1",
        version=version_stamp(child.updated_at),
    )
    async with database.sessions() as session:
        await validate_references(session, (ref,), owner_id=owner, privacy_level="L1")
    async with database.sessions.begin() as session:
        if change.endswith("intent"):
            session.add(
                DeletionLedgerRecord(
                    entity_kind="message",
                    entity_id=str(
                        conversation if change == "conversation_intent" else message
                    ).upper(),
                    deleted_ids=[],
                    requested_by="fixture",
                )
            )
        else:
            row = await session.get(MessageRecord, message)
            assert row
            if change == "private":
                row.privacy_level = "L2"
            else:
                await session.delete(row)
    async with database.sessions() as session:
        assert await session.get(MemoryRecord, child.id) is not None
        with pytest.raises(ContextSourceInvalidated):
            await validate_references(session, (ref,), owner_id=owner, privacy_level="L1")


async def test_captured_lineage_cannot_be_severed_without_invalidation(database: Database) -> None:
    from sqlalchemy import delete

    from app.context.repository import attach_memory_lineage
    from app.db import MemorySourceRecord

    owner, _, _ = await seed(database)
    memory = MemoryStore(database)
    entry = await memory.add(candidate("fixture lineage"), user_id=owner)
    reference = ContextReference(
        kind="memory",
        source_id=str(entry.id),
        owner_id=str(owner),
        privacy_level="L1",
        version=version_stamp(entry.updated_at),
    )
    async with database.sessions() as session:
        references = await attach_memory_lineage(session, (reference,), owner_id=owner)
        assert references[0].lineage
        await validate_references(session, references, owner_id=owner, privacy_level="L1")
    async with database.sessions.begin() as session:
        await session.execute(
            delete(MemorySourceRecord).where(MemorySourceRecord.memory_id == entry.id)
        )
    async with database.sessions() as session:
        assert await session.get(MemoryRecord, entry.id) is not None
        with pytest.raises(ContextSourceInvalidated):
            await validate_references(session, references, owner_id=owner, privacy_level="L1")


async def test_evidence_ids_without_owned_versions_do_not_reach_model(
    database: Database, tmp_path: Path
) -> None:
    owner, store, _ = await seed(database)
    memory = MemoryStore(database)
    entry = await memory.add(candidate("fixture unproven reference"), user_id=owner)
    event = SemanticEvent(
        event_id=uuid7(),
        user_id=owner,
        kind="fixture",
        summary="fixture",
        privacy_level=PrivacyLevel.L1,
        occurred_at=datetime.now(UTC),
        evidence_ids=["fixture"],
    )
    state = await WorldStateBuilder(database, store).build(event)
    state = state.model_copy(update={"memory_evidence_ids": [str(entry.id)]})

    class Backend:
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            raise AssertionError("unproven evidence must not reach a model")

    path = tmp_path / "config.yaml"
    path.write_text(config_yaml())
    config = ConfigStore(path)
    await config.load()
    deliberator = RouterDeliberator(config, database=database, router_builder=lambda _: Backend())
    with pytest.raises(BudgetDenied, match="model_source_changed"):
        await deliberator.deliberate(
            event,
            state,
            AttentionResult(
                score=1, threshold=0.5, reason_codes=["fixture"], should_deliberate=True
            ),
        )
    async with database.sessions() as session:
        assert not list(await session.scalars(select(TaskRunRecord)))


@pytest.mark.parametrize("target", ["message", "conversation"])
async def test_missing_original_timeline_still_honors_delete_intent(
    database: Database, target: str
) -> None:
    from app.context.repository import timeline_reference

    owner, _, _ = await seed(database)
    message, conversation = uuid7(), uuid7()
    entry = await TimelineStore(database).index_message(
        user_id=owner,
        conversation_id=conversation,
        message_id=message,
        actor=TimelineActor.USER,
        text="fixture retained summary",
        privacy_level=PrivacyLevel.L1,
        occurred_at=datetime.now(UTC),
    )
    assert entry
    references = (timeline_reference(entry),)
    async with database.sessions() as session:
        await validate_references(session, references, owner_id=owner, privacy_level="L1")
    async with database.sessions.begin() as session:
        session.add(
            DeletionLedgerRecord(
                entity_kind="message",
                entity_id=str(message if target == "message" else conversation),
                deleted_ids=[],
                requested_by="fixture",
            )
        )
    async with database.sessions() as session:
        assert await session.get(TimelineEventRecord, entry.id) is not None
        with pytest.raises(ContextSourceInvalidated):
            await validate_references(session, references, owner_id=owner, privacy_level="L1")
