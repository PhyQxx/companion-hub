"""Owned world facts preserve privacy and grants without exposing mapped records."""

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest
from test_cognitive_save_guard import database as save_database

from app.cognition import CognitiveStore, GoalView, SemanticEvent, WorldStateBuilder
from app.cognition.world_facts import WorldFacts
from app.cognition.world_sql import SqlWorldFactsRepository
from app.db import (
    AppUserRecord,
    CognitiveDecisionRecord,
    CognitiveFeedbackRecord,
    ConversationRecord,
    Database,
    DeviceClientRecord,
    MessageRecord,
)
from app.harness.context import ContextReference
from app.ids import uuid7
from app.schemas import PrivacyLevel

database = save_database


@pytest.mark.parametrize("privacy", [PrivacyLevel.L0, PrivacyLevel.L1])
async def test_facts_preserve_owned_message_privacy_and_current_device_grants(
    database: Database, privacy: PrivacyLevel
) -> None:
    now = datetime.now(UTC)
    owner, foreign, own_conversation, other_conversation = (uuid7() for _ in range(4))
    async with database.sessions.begin() as session:
        session.add_all(
            [
                AppUserRecord(id=owner, display_name="Fixture", status="active", timezone="UTC"),
                AppUserRecord(id=foreign, display_name="Other fixture", status="active"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                ConversationRecord(id=own_conversation, user_id=owner, status="active"),
                ConversationRecord(id=other_conversation, user_id=foreign, status="active"),
            ]
        )
        await session.flush()
        for index, (conversation, level) in enumerate(
            [(own_conversation, "L0"), (own_conversation, "L1"), (other_conversation, "L0")]
        ):
            session.add(
                MessageRecord(
                    id=uuid7(),
                    conversation_id=conversation,
                    turn_id=uuid7(),
                    seq=index + 1,
                    role="user",
                    content="fixture",
                    privacy_level=level,
                    created_at=now - timedelta(minutes=3 - index),
                )
            )
        for index, (user, seen, revoked, capabilities, granted) in enumerate(
            [
                (owner, now, None, ["allowed", "not_granted"], ["allowed", "not_declared"]),
                (owner, now, now, ["revoked"], ["revoked"]),
                (owner, now - timedelta(minutes=5), None, ["stale"], ["stale"]),
                (foreign, now, None, ["foreign"], ["foreign"]),
            ]
        ):
            session.add(
                DeviceClientRecord(
                    id=uuid7(),
                    owner_user_id=user,
                    name="Fixture",
                    client_type="fixture",
                    credential_hash=str(index),
                    capabilities=capabilities,
                    granted_capabilities=granted,
                    paired_at=now,
                    last_seen_at=seen,
                    revoked_at=revoked,
                )
            )
    facts = await SqlWorldFactsRepository(database).read(
        user_id=owner, trigger_kind="fixture", privacy_level=privacy, now=now
    )
    assert facts.timezone == "UTC"
    assert facts.last_interaction_at is not None
    assert facts.last_interaction_at.replace(tzinfo=UTC) == now - timedelta(
        minutes=3 if privacy == PrivacyLevel.L0 else 2
    )
    assert facts.active_capabilities == ("allowed",)


async def test_feedback_cannot_link_foreign_decision_into_owned_world(database: Database) -> None:
    owner, foreign, own_decision, other_decision = (uuid7() for _ in range(4))
    now = datetime.now(UTC)
    async with database.sessions.begin() as session:
        session.add_all(
            [
                AppUserRecord(id=owner, display_name="Fixture", status="active"),
                AppUserRecord(id=foreign, display_name="Other", status="active"),
            ]
        )
        await session.flush()
        for user, identifier, kind in [
            (owner, own_decision, "inform"),
            (owner, uuid7(), "ignore"),
            (foreign, other_decision, "inform"),
        ]:
            session.add(
                CognitiveDecisionRecord(
                    id=identifier,
                    user_id=user,
                    event_id=uuid7(),
                    trigger_kind="fixture",
                    decision=kind,
                    reason_codes=[],
                    evidence_ids=[],
                    confidence=1,
                    urgency="normal",
                    attention_score=1,
                    policy_version="fixture",
                    created_at=now,
                )
            )
        await session.flush()
        for user, decision in [
            (owner, own_decision),
            (owner, other_decision),
            (foreign, own_decision),
        ]:
            session.add(
                CognitiveFeedbackRecord(
                    id=uuid7(),
                    user_id=user,
                    decision_id=decision,
                    kind="ignored",
                    metadata_json={},
                    created_at=now,
                )
            )
    facts = await SqlWorldFactsRepository(database).read(
        user_id=owner, trigger_kind="fixture", privacy_level=PrivacyLevel.L1, now=now
    )
    assert facts.recent_proactive_count == 1
    assert facts.same_trigger_recent_count == 1
    assert facts.ignored_same_trigger_count == 1


async def test_world_assembly_can_use_only_detached_ports() -> None:
    now, owner = datetime.now(UTC), uuid7()

    class Repository:
        async def read(
            self, *, user_id: UUID, trigger_kind: str, privacy_level: PrivacyLevel, now: datetime
        ) -> WorldFacts:
            assert (
                user_id == owner and trigger_kind == "fixture" and privacy_level == PrivacyLevel.L0
            )
            return WorldFacts("UTC", None, ("fixture",), 1, 2, 3)

        async def attach_memory_lineage(
            self, references: tuple[ContextReference, ...], *, user_id: UUID
        ) -> tuple[ContextReference, ...]:
            raise AssertionError("no memory provider configured")

    class Goals:
        async def active_goals(
            self, user_id: UUID, *, now: datetime, max_privacy_level: PrivacyLevel
        ) -> list[GoalView]:
            assert user_id == owner and max_privacy_level == PrivacyLevel.L0
            return []

    # No engine/session exists: the injected facts and goal ports are sufficient.
    builder = WorldStateBuilder(
        cast(Database, object()), cast(CognitiveStore, Goals()), repository=Repository()
    )
    state = await builder.build(
        SemanticEvent(
            event_id=uuid7(),
            user_id=owner,
            kind="fixture",
            summary="fixture",
            privacy_level=PrivacyLevel.L0,
            occurred_at=now,
            evidence_ids=["fixture"],
        ),
        now=now,
    )
    assert state.active_capabilities == ["fixture"] and state.timezone == "UTC"
    assert (
        state.recent_proactive_count,
        state.same_trigger_recent_count,
        state.ignored_same_trigger_count,
    ) == (1, 2, 3)
