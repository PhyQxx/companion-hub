from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.cognition import (
    AttentionEngine,
    CognitiveCycle,
    CognitiveStore,
    DecisionKind,
    RuleBasedDeliberator,
    SemanticEvent,
    WorldStateBuilder,
)
from app.config import HomeAssistantEntityConfig
from app.db import (
    AppUserRecord,
    Base,
    CognitiveDecisionRecord,
    Database,
    SemanticEventAuditRecord,
    create_database,
)
from app.home_assistant.models import HomeAssistantState
from app.home_assistant.proactive import _semantic_transition
from app.ids import uuid7
from app.perception import (
    PerceptionDisposition,
    PerceptionPipeline,
    PerceptionStore,
    ProactivePolicy,
    ProactivePolicySettings,
)
from app.schemas import PrivacyLevel


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    result = create_database("sqlite+aiosqlite:///:memory:")
    async with result.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield result
    finally:
        await result.close()


@pytest.fixture
async def user_id(database: Database) -> UUID:
    value = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=value, display_name="Perception Eval", status="active"))
    return value


def create_pipeline(
    database: Database,
    *,
    daily_limit: int = 5,
    dedupe_window_seconds: int = 300,
) -> PerceptionPipeline:
    cognitive_store = CognitiveStore(database)
    cycle = CognitiveCycle(
        cognitive_store,
        WorldStateBuilder(database, cognitive_store),
        AttentionEngine(),
        RuleBasedDeliberator(),
    )
    perception_store = PerceptionStore(database)
    policy = ProactivePolicy(
        database,
        ProactivePolicySettings(
            quiet_hours_start="00:00",
            quiet_hours_end="00:00",
            daily_limit=daily_limit,
            dedupe_window_seconds=dedupe_window_seconds,
        ),
    )
    return PerceptionPipeline(cycle, perception_store, policy)


def event(
    user_id: UUID,
    kind: str,
    *,
    source_kind: str = "home_assistant",
    dedupe_key: str | None = None,
    dnd: bool = False,
    expires_at: datetime | None = None,
    privacy_level: PrivacyLevel = PrivacyLevel.L1,
) -> SemanticEvent:
    now = datetime.now(UTC)
    return SemanticEvent(
        event_id=uuid7(),
        user_id=user_id,
        kind=kind,
        source_kind=source_kind,
        dedupe_key=dedupe_key,
        summary=f"semantic event: {kind}",
        occurred_at=now,
        privacy_level=privacy_level,
        evidence_ids=[f"{source_kind}:window:{now.isoformat()}"],
        attributes={"dnd": dnd},
        expires_at=expires_at or now + timedelta(minutes=5),
    )


async def test_cross_source_events_merge_and_replay_is_idempotent(
    database: Database,
    user_id: UUID,
) -> None:
    pipeline = create_pipeline(database)
    first_event = event(
        user_id,
        "user_arrived_home",
        source_kind="home_assistant",
        dedupe_key=f"user:{user_id}:arrival",
    )
    duplicate = event(
        user_id,
        "user_arrived_home",
        source_kind="mqtt_presence",
        dedupe_key=f"user:{user_id}:arrival",
    )

    first = await pipeline.process(first_event)
    merged = await pipeline.process(duplicate)
    replay = await pipeline.process(first_event)

    assert first.disposition == PerceptionDisposition.PROCESSED
    assert first.decision is not None
    assert first.decision.decision == DecisionKind.SUGGEST
    assert merged.disposition == PerceptionDisposition.MERGED
    assert merged.merged_into_event_id == first_event.event_id
    assert replay.reason_code == "event_already_processed"
    async with database.sessions() as session:
        decisions = await session.scalar(select(func.count(CognitiveDecisionRecord.id)))
        audits = await session.scalar(select(func.count(SemanticEventAuditRecord.event_id)))
    assert decisions == 1
    assert audits == 2


async def test_concurrent_cross_source_events_only_deliberate_once(
    database: Database,
    user_id: UUID,
) -> None:
    pipeline = create_pipeline(database)
    dedupe_key = f"user:{user_id}:arrival"
    results = await asyncio.gather(
        *[
            pipeline.process(
                event(user_id, "user_arrived_home", source_kind=source, dedupe_key=dedupe_key)
            )
            for source in ("home_assistant", "mqtt_presence", "mobile_geofence")
        ]
    )

    assert sum(item.disposition == PerceptionDisposition.PROCESSED for item in results) == 1
    assert sum(item.disposition == PerceptionDisposition.MERGED for item in results) == 2
    async with database.sessions() as session:
        decisions = await session.scalar(select(func.count(CognitiveDecisionRecord.id)))
    assert decisions == 1


async def test_global_policy_handles_dnd_daily_budget_and_critical_bypass(
    database: Database,
    user_id: UUID,
) -> None:
    pipeline = create_pipeline(database, daily_limit=1, dedupe_window_seconds=1)
    dnd = await pipeline.process(event(user_id, "temperature_high", dnd=True))
    first = await pipeline.process(event(user_id, "user_arrived_home"))
    limited = await pipeline.process(
        event(user_id, "light_on_too_long", dedupe_key="light:kitchen")
    )
    critical = await pipeline.process(
        event(user_id, "water_leak", dedupe_key="leak:kitchen", dnd=True)
    )

    assert dnd.disposition == PerceptionDisposition.SUPPRESSED
    assert dnd.reason_code == "dnd"
    assert first.disposition == PerceptionDisposition.PROCESSED
    assert limited.reason_code == "daily_limit"
    assert critical.disposition == PerceptionDisposition.PROCESSED
    assert critical.decision is not None
    assert critical.decision.decision == DecisionKind.ESCALATE


async def test_expired_and_l3_events_never_create_durable_raw_audit(
    database: Database,
    user_id: UUID,
) -> None:
    pipeline = create_pipeline(database)
    expired = await pipeline.process(
        event(user_id, "user_arrived_home", expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    private = await pipeline.process(
        event(user_id, "presence.changed", privacy_level=PrivacyLevel.L3)
    )

    assert expired.disposition == PerceptionDisposition.EXPIRED
    assert private.disposition == PerceptionDisposition.PROCESSED
    async with database.sessions() as session:
        audits = await session.scalar(select(func.count(SemanticEventAuditRecord.event_id)))
        decisions = await session.scalar(select(func.count(CognitiveDecisionRecord.id)))
    assert audits == 1
    assert decisions == 0


async def test_stability_window_rejects_changed_state_before_cognition(
    database: Database,
    user_id: UUID,
) -> None:
    pipeline = create_pipeline(database)
    completed = asyncio.Event()
    captured = []

    async def handler(semantic: SemanticEvent, result) -> None:  # type: ignore[no-untyped-def]
        captured.append((semantic, result))
        completed.set()

    pipeline.submit(
        event(user_id, "presence.changed", dedupe_key="presence:office"),
        stable_for_seconds=0.01,
        validate=lambda: False,
        handler=handler,
    )
    await asyncio.wait_for(completed.wait(), timeout=1)
    await pipeline.stop()

    assert captured[0][1].disposition == PerceptionDisposition.UNSTABLE
    async with database.sessions() as session:
        decisions = await session.scalar(select(func.count(CognitiveDecisionRecord.id)))
    assert decisions == 0


def test_home_assistant_transition_mapper_emits_only_semantic_changes() -> None:
    person = HomeAssistantEntityConfig(
        entity_id="person.owner",
        display_name="主人",
        read_allowed=True,
    )
    old = HomeAssistantState("person.owner", "not_home", {}, None, None)
    home = HomeAssistantState("person.owner", "home", {}, None, None)
    assert _semantic_transition(person, old, home) is not None
    assert _semantic_transition(person, home, home) is None

    presence = HomeAssistantEntityConfig(
        entity_id="binary_sensor.office_presence",
        display_name="书房存在",
        read_allowed=True,
    )
    absent = HomeAssistantState(
        presence.entity_id,
        "off",
        {"device_class": "occupancy"},
        None,
        None,
    )
    present = HomeAssistantState(
        presence.entity_id,
        "on",
        {"device_class": "occupancy"},
        None,
        None,
    )
    mapped = _semantic_transition(presence, absent, present)
    assert mapped is not None
    assert mapped[0] == "presence.changed"
