# ruff: noqa: RUF001
"""Deterministic cognitive decisions without provider or persistence imports."""

from __future__ import annotations

from app.ids import uuid7

from .models import (
    AttentionResult,
    CognitiveDecision,
    DecisionKind,
    SemanticEvent,
    Urgency,
    WorldState,
)
from .ports import COGNITIVE_POLICY_VERSION
from .proactive import CRITICAL_EVENTS


class RuleBasedDeliberator:
    async def deliberate(
        self, event: SemanticEvent, state: WorldState, attention: AttentionResult
    ) -> CognitiveDecision:
        decision = DecisionKind.RECORD
        urgency = Urgency.LOW
        message: str | None = None
        reasons = [*attention.reason_codes]
        if event.passive:
            reasons.append("passive_response_required")
        elif event.kind in CRITICAL_EVENTS:
            decision = DecisionKind.ESCALATE
            urgency = Urgency.CRITICAL
            raw_message = event.attributes.get("message")
            message = raw_message if isinstance(raw_message, str) else None
            message = message or "检测到水浸或安全告警，请立即检查现场。"
            reasons.append("critical_safety_event")
        elif event.kind == "user_arrived_home":
            decision = DecisionKind.SUGGEST
            urgency = Urgency.NORMAL
            message = "欢迎回家。我已经看到家里的状态，需要我帮你检查灯光或温度吗？"
            reasons.append("presence_confirmed")
        elif event.kind == "light_on_too_long":
            decision = DecisionKind.ASK
            urgency = Urgency.NORMAL
            message = "有一盏灯已经开了较长时间，需要我帮你关闭吗？"
            reasons.append("sustained_state")
        elif event.kind == "temperature_high":
            decision = DecisionKind.SUGGEST
            urgency = Urgency.NORMAL
            message = "室内温度持续偏高，需要我帮你调整空调吗？"
            reasons.append("sustained_state")
        else:
            decision = DecisionKind.INFORM
            urgency = Urgency.NORMAL
            message = event.summary
        return CognitiveDecision(
            id=uuid7(),
            event_id=event.event_id,
            user_id=event.user_id,
            conversation_id=event.conversation_id,
            trigger_kind=event.kind,
            decision=decision,
            reason_codes=list(dict.fromkeys(reasons)),
            evidence_ids=event.evidence_ids,
            confidence=event.confidence,
            urgency=urgency,
            attention_score=attention.score,
            policy_version=COGNITIVE_POLICY_VERSION,
            message=message,
            approval_required=decision in {DecisionKind.ASK, DecisionKind.SUGGEST},
            expires_at=event.expires_at,
            created_at=state.built_at,
        )
