from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.cognition import (
    ActionEngine,
    AttentionEngine,
    CognitiveCycle,
    CognitiveStore,
    DecisionKind,
    FeedbackKind,
    GoalKind,
    GoalStatus,
    RouterDeliberator,
    RuleBasedDeliberator,
    SemanticEvent,
    WorldStateBuilder,
)
from app.config import ConfigStore
from app.db import AppUserRecord, Base, CognitiveDecisionRecord, Database, create_database
from app.ids import uuid7
from app.llm import CompletionRequest, CompletionResult, LLMRoute, ModelUsage
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
        session.add(AppUserRecord(id=value, display_name="M3B Eval", status="active"))
    return value


@pytest.fixture
def cognitive_cycle(database: Database) -> CognitiveCycle:
    store = CognitiveStore(database)
    return CognitiveCycle(
        store,
        WorldStateBuilder(database, store),
        AttentionEngine(),
        RuleBasedDeliberator(),
    )


def semantic_event(
    user_id: UUID,
    kind: str,
    *,
    dnd: bool = False,
    privacy_level: PrivacyLevel = PrivacyLevel.L1,
) -> SemanticEvent:
    now = datetime.now(UTC)
    return SemanticEvent(
        event_id=uuid7(),
        user_id=user_id,
        kind=kind,
        summary=f"eval event: {kind}",
        occurred_at=now,
        privacy_level=privacy_level,
        evidence_ids=[f"sensor:{kind}:{now.isoformat()}"],
        attributes={"dnd": dnd},
        expires_at=now + timedelta(minutes=5),
    )


class FakeCognitiveBackend:
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        return CompletionResult(
            text=self.text,
            provider="eval-provider",
            model="eval-model",
            endpoint="eval",
            route=LLMRoute.DIALOGUE,
            finish_reason="stop",
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            latency_ms=1,
        )


async def test_arrival_respects_dnd_and_duplicate_frequency_reduction(
    cognitive_cycle: CognitiveCycle,
    user_id: UUID,
) -> None:
    dnd = await cognitive_cycle.evaluate(semantic_event(user_id, "user_arrived_home", dnd=True))
    assert dnd.decision == DecisionKind.IGNORE
    assert "dnd" in dnd.reason_codes

    first = await cognitive_cycle.evaluate(semantic_event(user_id, "user_arrived_home"))
    second = await cognitive_cycle.evaluate(semantic_event(user_id, "user_arrived_home"))

    assert first.decision == DecisionKind.SUGGEST
    assert second.decision == DecisionKind.IGNORE
    assert "duplicate_penalty" in second.reason_codes


async def test_light_asks_and_water_leak_escalates_even_during_dnd(
    cognitive_cycle: CognitiveCycle,
    user_id: UUID,
) -> None:
    light = await cognitive_cycle.evaluate(semantic_event(user_id, "light_on_too_long"))
    leak = await cognitive_cycle.evaluate(semantic_event(user_id, "water_leak", dnd=True))

    assert light.decision == DecisionKind.ASK
    assert light.approval_required is True
    assert leak.decision == DecisionKind.ESCALATE
    assert leak.urgency == "critical"
    assert leak.message


async def test_passive_and_l3_events_share_cycle_without_persisting_private_raw_state(
    cognitive_cycle: CognitiveCycle,
    database: Database,
    user_id: UUID,
) -> None:
    now = datetime.now(UTC)
    passive = await cognitive_cycle.evaluate(
        SemanticEvent(
            event_id=uuid7(),
            user_id=user_id,
            kind="message.received",
            summary="user sent a message",
            occurred_at=now,
            privacy_level=PrivacyLevel.L1,
            evidence_ids=["message:example"],
            passive=True,
        )
    )
    private = await cognitive_cycle.evaluate(
        semantic_event(user_id, "water_leak", privacy_level=PrivacyLevel.L3)
    )

    assert passive.decision == DecisionKind.RECORD
    assert private.decision == DecisionKind.ESCALATE
    async with database.sessions() as session:
        count = await session.scalar(select(func.count(CognitiveDecisionRecord.id)))
    assert count == 1


async def test_feedback_creates_traceable_reflection_candidate(
    cognitive_cycle: CognitiveCycle,
    user_id: UUID,
) -> None:
    decisions = [
        await cognitive_cycle.evaluate(semantic_event(user_id, "light_on_too_long"))
        for _ in range(3)
    ]
    for decision in decisions:
        await cognitive_cycle.store.add_feedback(
            user_id=user_id,
            decision_id=decision.id,
            kind=FeedbackKind.IGNORED,
        )

    candidate = await cognitive_cycle.store.reflection_candidate(
        user_id=user_id,
        trigger_kind="light_on_too_long",
    )

    assert candidate is not None
    assert len(candidate.evidence_ids) == 3
    assert candidate.requires_confirmation is True


async def test_goal_requires_explicit_source_and_supports_cancellation(
    cognitive_cycle: CognitiveCycle,
    user_id: UUID,
) -> None:
    with pytest.raises(ValueError, match="explicit user or manual evidence"):
        await cognitive_cycle.store.create_goal(
            user_id=user_id,
            kind=GoalKind.USER,
            title="每天运动",
            source_kind="model",
            source_id="inference-1",
        )

    goal = await cognitive_cycle.store.create_goal(
        user_id=user_id,
        kind=GoalKind.USER,
        title="每天运动",
        source_kind="manual",
        source_id="api-request",
    )
    cancelled = await cognitive_cycle.store.set_goal_status(
        user_id=user_id,
        goal_id=goal.id,
        status=GoalStatus.CANCELLED,
    )
    assert cancelled.status == GoalStatus.CANCELLED


async def test_no_evidence_cannot_enter_proactive_or_action_path(
    cognitive_cycle: CognitiveCycle,
    user_id: UUID,
) -> None:
    with pytest.raises(ValidationError, match="require evidence"):
        SemanticEvent(
            event_id=uuid7(),
            user_id=user_id,
            kind="light_on_too_long",
            summary="unsupported assertion",
            occurred_at=datetime.now(UTC),
            privacy_level=PrivacyLevel.L1,
        )

    decision = await cognitive_cycle.evaluate(semantic_event(user_id, "light_on_too_long"))
    result = await ActionEngine().execute(decision)
    assert result.outcome == "not_applicable"
    assert result.verified is True


async def test_model_deliberation_is_structured_and_cannot_enable_act(
    database: Database,
    user_id: UUID,
) -> None:
    config_store = cast(
        ConfigStore,
        SimpleNamespace(current=SimpleNamespace(config=object())),
    )
    valid_backend = FakeCognitiveBackend(
        '{"decision":"inform","reason_codes":["model_relevant"],'
        '"confidence":0.8,"urgency":"normal","message":"欢迎回家。"}'
    )
    cognitive_store = CognitiveStore(database)
    cycle = CognitiveCycle(
        cognitive_store,
        WorldStateBuilder(database, cognitive_store),
        AttentionEngine(),
        RouterDeliberator(
            config_store,
            router_builder=lambda config: valid_backend,
        ),
    )

    valid = await cycle.evaluate(semantic_event(user_id, "user_arrived_home"))
    assert valid.decision == DecisionKind.INFORM
    assert valid.model_provider == "eval-provider"
    assert valid_backend.requests[0].json_mode is True

    blocked_backend = FakeCognitiveBackend(
        '{"decision":"act","reason_codes":["unsafe"],'
        '"confidence":1,"urgency":"normal"}'
    )
    safe_cycle = CognitiveCycle(
        cognitive_store,
        WorldStateBuilder(database, cognitive_store),
        AttentionEngine(threshold=0),
        RouterDeliberator(
            config_store,
            router_builder=lambda config: blocked_backend,
        ),
    )
    blocked = await safe_cycle.evaluate(semantic_event(user_id, "light_on_too_long"))
    assert blocked.decision == DecisionKind.ASK
    assert blocked.model_provider is None
