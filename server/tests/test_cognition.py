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
    ActionLevel,
    ActionOutcome,
    AttentionEngine,
    CognitiveCycle,
    CognitiveStore,
    DecisionKind,
    FeedbackKind,
    GoalKind,
    GoalStatus,
    ReflectionEngine,
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
    third = await cognitive_cycle.evaluate(semantic_event(user_id, "user_arrived_home"))

    assert first.decision == DecisionKind.SUGGEST
    # 重复惩罚只统计浮出水面的决策：一次 SUGGEST 后窗口内还有一次机会
    # （0.7+0.05-0.15=0.60 ≥ 0.55），第二次 SUGGEST 后（-0.30）被压回 ignore。
    # 高频事件源（屏幕感知）的 below-threshold 静默不再累积惩罚把通道永久压死。
    assert second.decision == DecisionKind.SUGGEST
    assert third.decision == DecisionKind.IGNORE
    assert "duplicate_penalty" in third.reason_codes


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
    assert result.outcome == ActionOutcome.PROMPTED
    assert result.verified is False


async def test_action_engine_maps_decisions_to_correct_levels() -> None:
    engine = ActionEngine()
    from app.cognition import CognitiveDecision, Urgency

    now = datetime.now(UTC)

    def make_decision(kind: DecisionKind) -> CognitiveDecision:
        # Use model_construct to bypass the deliberation-level ACT guard
        # so we can test the action-layer mapping independently.
        return CognitiveDecision.model_construct(
            id=uuid7(),
            event_id=uuid7(),
            user_id=uuid7(),
            trigger_kind="test",
            decision=kind,
            reason_codes=["test"],
            evidence_ids=["ev1"],
            confidence=0.9,
            urgency=Urgency.NORMAL,
            attention_score=0.7,
            policy_version="test",
            message="test message" if kind != DecisionKind.IGNORE else None,
            approval_required=kind in {DecisionKind.ASK, DecisionKind.SUGGEST},
            expires_at=now + timedelta(minutes=5),
            created_at=now,
        )

    plan_ignore = engine.plan(make_decision(DecisionKind.IGNORE))
    assert plan_ignore.level == ActionLevel.A0_OBSERVE

    plan_suggest = engine.plan(make_decision(DecisionKind.SUGGEST))
    assert plan_suggest.level == ActionLevel.A1_PROMPT

    plan_act = engine.plan(make_decision(DecisionKind.ACT))
    assert plan_act.level == ActionLevel.A2_PREAUTHORIZED


async def test_action_engine_blocks_a2_and_a3_in_v1() -> None:
    engine = ActionEngine()
    from app.cognition import CognitiveDecision, Urgency

    now = datetime.now(UTC)
    act_decision = CognitiveDecision.model_construct(
        id=uuid7(),
        event_id=uuid7(),
        user_id=uuid7(),
        trigger_kind="test",
        decision=DecisionKind.ACT,
        reason_codes=["unsafe"],
        evidence_ids=["ev1"],
        confidence=1.0,
        urgency=Urgency.HIGH,
        attention_score=0.9,
        policy_version="test",
        expires_at=now + timedelta(minutes=5),
        created_at=now,
    )
    result = await engine.execute(act_decision)
    assert result.outcome == ActionOutcome.BLOCKED
    assert result.reason_code == "autonomous_action_disabled"
    assert result.verified is True


async def test_action_engine_records_outcome() -> None:
    engine = ActionEngine()

    result = await engine.record_outcome(
        uuid7(),
        outcome=ActionOutcome.VERIFIED,
        reason_code="delivery_confirmed",
        verified=True,
        observed_state={"channel": "web_chat"},
    )
    assert result.outcome == ActionOutcome.VERIFIED
    assert result.verified is True
    assert result.observed_state == {"channel": "web_chat"}


async def test_reflection_engine_generates_candidates_from_feedback(
    cognitive_cycle: CognitiveCycle,
    user_id: UUID,
) -> None:
    # Create 3 decisions with ignored feedback.
    decisions = [
        await cognitive_cycle.evaluate(semantic_event(user_id, "temperature_high"))
        for _ in range(3)
    ]
    for decision in decisions:
        await cognitive_cycle.store.add_feedback(
            user_id=user_id,
            decision_id=decision.id,
            kind=FeedbackKind.IGNORED,
        )

    engine = ReflectionEngine(cognitive_cycle.store)
    candidates = await engine.run_for_user(user_id)

    assert len(candidates) >= 1
    assert any("忽略率较高" in c.content for c in candidates)
    assert all(c.requires_confirmation for c in candidates)


async def test_reflection_engine_respects_min_total_threshold(
    cognitive_cycle: CognitiveCycle,
    user_id: UUID,
) -> None:
    # Only 2 feedbacks, below default min_total=3.
    decisions = [
        await cognitive_cycle.evaluate(semantic_event(user_id, "device_offline"))
        for _ in range(2)
    ]
    for decision in decisions:
        await cognitive_cycle.store.add_feedback(
            user_id=user_id,
            decision_id=decision.id,
            kind=FeedbackKind.IGNORED,
        )

    engine = ReflectionEngine(cognitive_cycle.store, min_total=3)
    candidates = await engine.run_for_user(user_id)
    assert candidates == []


async def test_reflection_engine_detects_forbidden() -> None:
    from app.cognition.reflection import FeedbackSummary, ReflectionEngine

    engine = ReflectionEngine(
        None, forbidden_threshold=2, min_total=2  # type: ignore[arg-type]
    )
    summary = FeedbackSummary(
        total=2, accepted=0, ignored=0, snoozed=0, forbidden=2, acceptance_rate=0.0
    )
    candidate = engine._analyse(summary, trigger_kind="light_on_too_long")
    assert candidate is not None
    assert "明确禁止" in candidate.content
    assert candidate.requires_confirmation is True


async def test_store_saves_and_retrieves_action_results(
    cognitive_cycle: CognitiveCycle,
    user_id: UUID,
) -> None:
    decision = await cognitive_cycle.evaluate(semantic_event(user_id, "light_on_too_long"))
    result = await ActionEngine().execute(decision)
    await cognitive_cycle.store.save_action_result(result, user_id=user_id)

    results = await cognitive_cycle.store.recent_action_results(user_id, limit=10)
    assert len(results) == 1
    assert results[0].outcome == ActionOutcome.PROMPTED


async def test_store_saves_and_lists_reflection_candidates(
    cognitive_cycle: CognitiveCycle,
    user_id: UUID,
) -> None:
    from app.cognition import ReflectionCandidate

    candidate_id = await cognitive_cycle.store.save_candidate(
        user_id=user_id,
        candidate=ReflectionCandidate(
            content="test candidate",
            evidence_ids=["ev1"],
            confidence=0.8,
            requires_confirmation=True,
        ),
    )
    assert candidate_id is not None

    pending = await cognitive_cycle.store.pending_candidates(user_id, limit=10)
    assert len(pending) == 1
    assert pending[0].content == "test candidate"


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


def test_attention_honors_source_salience_and_never_lowers() -> None:
    from datetime import UTC, datetime, timedelta

    from app.cognition.models import WorldState

    engine = AttentionEngine()
    now = datetime.now(UTC)
    state = WorldState(built_at=now, timezone="UTC")

    def event(**attributes: object) -> SemanticEvent:
        return SemanticEvent(
            event_id=uuid7(),
            user_id=uuid7(),
            kind="screen.observed",
            summary="屏幕出现会议提醒",
            occurred_at=now,
            privacy_level=PrivacyLevel.L1,
            confidence=0.8,
            evidence_ids=["screen:test"],
            attributes=attributes,
            expires_at=now + timedelta(minutes=5),
        )

    # 默认基础分 0.4：不带 salience 到不了阈值
    plain = engine.evaluate(event(message="hello"), state)
    assert plain.should_deliberate is False
    # 自带 salience 0.75：0.75*0.8+0.05=0.65 过阈值，reason 含 source_salience
    notable = engine.evaluate(event(message="会议提醒", salience=0.75), state)
    assert notable.should_deliberate is True
    assert notable.score == pytest.approx(0.65)
    assert "source_salience" in notable.reason_codes
    # 显著性低于表值时不能反向压低（水浸 1.0 不受影响）
    leak = semantic_event(uuid7(), "water_leak")
    leak = leak.model_copy(update={"attributes": {"salience": 0.2}})
    leak_result = engine.evaluate(leak, state)
    assert leak_result.score >= 1.0 * leak.confidence - 0.001
    assert "source_salience" not in leak_result.reason_codes
    # 非法 salience（越界/非数值）被忽略
    invalid = engine.evaluate(event(message="x", salience=1.5), state)
    assert invalid.score == plain.score
