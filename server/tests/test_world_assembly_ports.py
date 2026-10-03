"""World assembly uses detached ports and preserves grounded SQL parity."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

import pytest
from test_cognitive_save_guard import database as save_database
from test_memory import candidate
from test_perception import event
from test_perception import user_id as perception_user

from app.cognition import CognitiveStore, GoalKind, WorldStateBuilder
from app.cognition.models import GoalView
from app.cognition.world_assembly import WorldAssembler
from app.cognition.world_facts import WorldFacts
from app.cognition.world_sql import SqlWorldFactsRepository
from app.context.snapshots import version_stamp
from app.db import Database
from app.harness.context import ContextReference
from app.ids import uuid7
from app.memory import MemoryRetriever, MemoryStore
from app.memory.retrieval import MemoryHit as LegacyHit
from app.memory.retrieval import RetrievalResult as LegacyResult
from app.memory.retrieval_models import MemoryHit, RetrievalResult
from app.schemas import PrivacyLevel
from app.timeline import TimelineActor, TimelineSourceType, TimelineStore
from app.timeline.models import TimelineSearchResult

database = save_database
user_id = perception_user


class Facts:
    async def read(
        self, *, user_id: UUID, trigger_kind: str, privacy_level: PrivacyLevel, now: datetime
    ) -> WorldFacts:
        return WorldFacts("UTC", None, ("fixture",), 3, 2, 1)

    async def attach_memory_lineage(
        self, references: tuple[ContextReference, ...], *, user_id: UUID
    ) -> tuple[ContextReference, ...]:
        return references


class Goals:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, PrivacyLevel | None]] = []

    async def active_goals(
        self, user_id: UUID, *, now: datetime, max_privacy_level: PrivacyLevel | None = None
    ) -> list[GoalView]:
        self.calls.append((user_id, max_privacy_level))
        return []


class Memories:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[UUID, PrivacyLevel]] = []
        self.fail = fail

    async def retrieve(
        self, query: str, *, user_id: UUID, privacy_level: PrivacyLevel, now: datetime | None = None
    ) -> RetrievalResult:
        self.calls.append((user_id, privacy_level))
        if self.fail:
            raise RuntimeError("fixture source unavailable")
        return RetrievalResult((), "fixture", 0, 0, 0)


class Timeline:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[UUID, tuple[PrivacyLevel, ...]]] = []
        self.fail = fail

    async def search(
        self,
        *,
        user_id: UUID,
        query: str = "",
        privacy_levels: Sequence[PrivacyLevel] = (PrivacyLevel.L0, PrivacyLevel.L1),
        limit: int = 20,
    ) -> TimelineSearchResult:
        self.calls.append((user_id, tuple(privacy_levels)))
        if self.fail:
            raise RuntimeError("fixture source unavailable")
        return TimelineSearchResult((), 0)


@pytest.mark.parametrize("privacy", [PrivacyLevel.L1, PrivacyLevel.L2, PrivacyLevel.L3])
async def test_memory_ports_assemble_without_database_and_respect_private_queries(
    privacy: PrivacyLevel,
) -> None:
    goals, memories, timeline = Goals(), Memories(), Timeline()
    source = event(uuid7(), "unknown_probe", privacy_level=privacy)
    moment = datetime.now(UTC)
    result = await WorldAssembler(
        Facts(), goals, memory_retriever=memories, timeline_store=timeline
    ).build(source, now=moment)
    assert result.built_at == moment and result.timezone == "UTC"
    assert result.active_capabilities == ["fixture"]
    assert (
        result.recent_proactive_count,
        result.same_trigger_recent_count,
        result.ignored_same_trigger_count,
    ) == (3, 2, 1)
    assert goals.calls == [(source.user_id, privacy)]
    assert len(memories.calls) == len(timeline.calls) == (0 if privacy == PrivacyLevel.L3 else 1)
    if privacy != PrivacyLevel.L3:
        assert memories.calls == [(source.user_id, privacy)]
        assert all(level <= privacy for _, levels in timeline.calls for level in levels)
    assert not result.context_references


@pytest.mark.parametrize("failed", ["memory", "timeline", "both"])
async def test_optional_query_failure_preserves_facts_and_goals(failed: str) -> None:
    goals = Goals()
    source = event(uuid7(), "unknown_probe")
    result = await WorldAssembler(
        Facts(),
        goals,
        memory_retriever=Memories(fail=failed != "timeline"),
        timeline_store=Timeline(fail=failed != "memory"),
    ).build(source)
    assert result.active_capabilities == ["fixture"] and len(goals.calls) == 1
    assert not result.memory_evidence_ids and not result.timeline_evidence_ids
    assert not result.context_references


async def test_world_snapshots_event_attributes_before_fact_wait() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class DelayedFacts(Facts):
        async def read(
            self, *, user_id: UUID, trigger_kind: str, privacy_level: PrivacyLevel, now: datetime
        ) -> WorldFacts:
            entered.set()
            await release.wait()
            return await super().read(
                user_id=user_id, trigger_kind=trigger_kind, privacy_level=privacy_level, now=now
            )

    source = event(uuid7(), "unknown_probe")
    task = asyncio.create_task(WorldAssembler(DelayedFacts(), Goals()).build(source))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        source.attributes["dnd"] = True
        release.set()
        assert not (await task).dnd
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "privacy", [PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2, PrivacyLevel.L3]
)
async def test_pure_world_and_legacy_sql_composition_preserve_grounding(
    database: Database, user_id: UUID, privacy: PrivacyLevel
) -> None:
    cognitive, memories, timeline = (
        CognitiveStore(database),
        MemoryStore(database),
        TimelineStore(database),
    )
    await memories.add(candidate("fixture cilantro preference"), user_id=user_id)
    await timeline.index_custom(
        user_id=user_id,
        source_id="fixture",
        source_type=TimelineSourceType.SYSTEM,
        actor=TimelineActor.SYSTEM,
        event_type="fixture",
        occurred_at=datetime.now(UTC),
        title="fixture cilantro preference",
        summary="fixture cilantro preference",
        privacy_level=PrivacyLevel.L1,
    )
    await cognitive.create_goal(
        user_id=user_id,
        kind=GoalKind.USER,
        title="fixture",
        source_kind="manual",
        source_id="fixture",
    )
    source = event(user_id, "unknown_probe", privacy_level=privacy).model_copy(
        update={"summary": "fixture cilantro preference"}
    )
    retriever = MemoryRetriever(memories)
    legacy = WorldStateBuilder(
        database, cognitive, memory_retriever=retriever, timeline_store=timeline
    )
    pure = WorldAssembler(
        SqlWorldFactsRepository(database),
        cognitive,
        memory_retriever=retriever,
        timeline_store=timeline,
    )
    moment = datetime.now(UTC)
    before, after = await legacy.build(source, now=moment), await pure.build(source, now=moment)
    assert before == after
    assert before.context_references == after.context_references
    if privacy in {PrivacyLevel.L1, PrivacyLevel.L2}:
        assert after.memory_evidence_ids and after.timeline_evidence_ids and after.active_goals
    else:
        assert not after.context_references


def test_retrieval_snapshot_exports_preserve_legacy_type_identity() -> None:
    assert LegacyHit is MemoryHit
    assert LegacyResult is RetrievalResult


@pytest.mark.parametrize("aware", [False, True])
def test_snapshot_versions_keep_utc_normalization(aware: bool) -> None:
    value = datetime(2026, 10, 3, 12, 0, tzinfo=UTC if aware else None)
    assert version_stamp(value) == "2026-10-03T12:00:00+00:00"
