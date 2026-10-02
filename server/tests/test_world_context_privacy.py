from datetime import UTC, datetime, timedelta

import pytest
from test_goal_delivery_runs import database as goal_database
from test_goal_delivery_runs import seed
from test_memory import candidate

from app.cognition import SemanticEvent, WorldStateBuilder
from app.db import Database
from app.ids import uuid7
from app.memory import MemoryRetriever, MemoryStore
from app.schemas import PrivacyLevel
from app.timeline import TimelineActor, TimelineSourceType, TimelineStore

database = goal_database


@pytest.mark.parametrize(
    "privacy,expected",
    [
        (PrivacyLevel.L0, {"L0"}),
        (PrivacyLevel.L1, {"L0", "L1"}),
        (PrivacyLevel.L2, {"L0", "L1", "L2"}),
        (PrivacyLevel.L3, set()),
    ],
)
async def test_world_context_never_widens_evidence_privacy(
    database: Database, privacy: PrivacyLevel, expected: set[str]
) -> None:
    owner, store, _ = await seed(database)
    timeline, memory = TimelineStore(database), MemoryStore(database)
    memory_levels: dict[str, str] = {}
    timeline_levels: dict[str, str] = {}
    for level in (PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2):
        entry = await memory.add(
            candidate(f"fixture cilantro preference {level}", privacy_level=level), user_id=owner
        )
        memory_levels[str(entry.id)] = level.value
        indexed = await timeline.index_custom(
            user_id=owner,
            source_id=f"fixture:{level}",
            source_type=TimelineSourceType.SYSTEM,
            actor=TimelineActor.SYSTEM,
            event_type="fixture",
            title="fixture",
            summary=f"fixture cilantro preference {level}",
            privacy_level=level,
            occurred_at=datetime.now(UTC),
        )
        assert indexed
        timeline_levels[str(indexed.id)] = level.value
    event = SemanticEvent(
        event_id=uuid7(),
        user_id=owner,
        kind="fixture",
        summary="fixture cilantro preference",
        occurred_at=datetime.now(UTC),
        privacy_level=privacy,
        confidence=1,
        evidence_ids=["fixture"],
    )
    state = await WorldStateBuilder(
        database, store, memory_retriever=MemoryRetriever(memory), timeline_store=timeline
    ).build(event, now=datetime.now(UTC) + timedelta(minutes=1))
    assert {memory_levels[identifier] for identifier in state.memory_evidence_ids} == expected
    assert {timeline_levels[identifier] for identifier in state.timeline_evidence_ids} == expected
