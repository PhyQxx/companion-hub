"""Structured decision policy depends on a completion port, never its adapter."""

from __future__ import annotations

import json

from app.harness.budget import BudgetDenied
from app.ids import uuid7
from app.schemas import PrivacyLevel

from .models import (
    AttentionResult,
    CognitiveDecision,
    DecisionKind,
    SemanticEvent,
    Urgency,
    WorldState,
)
from .ports import COGNITIVE_POLICY_VERSION, DeliberationCompletionPort, Deliberator
from .rule import RuleBasedDeliberator

TERMINAL_COMPLETION_REJECTIONS = frozenset(
    {
        "budget_run_inactive",
        "run_source_not_found",
        "model_run_owner_missing",
        "budget_owner_invalid",
        "model_source_changed",
        "model_source_check_failed",
        "model_run_source_already_processed",
        "run_deadline_exceeded",
    }
)


class StructuredDeliberator:
    def __init__(
        self, completion: DeliberationCompletionPort, *, fallback: Deliberator | None = None
    ) -> None:
        self._completion = completion
        self._fallback = fallback or RuleBasedDeliberator()

    async def deliberate(
        self, event: SemanticEvent, state: WorldState, attention: AttentionResult
    ) -> CognitiveDecision:
        event = event.model_copy(deep=True)
        state = state.model_copy(deep=True)
        attention = attention.model_copy(deep=True)
        if event.passive or event.privacy_level == PrivacyLevel.L3:
            return await self._fallback.deliberate(event, state, attention)
        try:
            result = await self._completion.complete(
                event.model_copy(deep=True),
                state.model_copy(deep=True),
                attention.model_copy(deep=True),
            )
            payload = json.loads(result.text)
            decision = DecisionKind(payload["decision"])
            if decision == DecisionKind.ACT:
                raise ValueError("act is disabled")
            message = str(payload.get("message") or "").strip() or None
            return CognitiveDecision(
                id=uuid7(),
                event_id=event.event_id,
                user_id=event.user_id,
                conversation_id=event.conversation_id,
                trigger_kind=event.kind,
                decision=decision,
                reason_codes=[str(item)[:80] for item in payload.get("reason_codes", [])][:8],
                evidence_ids=event.evidence_ids,
                confidence=float(payload.get("confidence", event.confidence)),
                urgency=Urgency(payload.get("urgency", "normal")),
                attention_score=attention.score,
                policy_version=COGNITIVE_POLICY_VERSION,
                message=message,
                approval_required=decision in {DecisionKind.ASK, DecisionKind.SUGGEST},
                model_provider=result.provider,
                model_name=result.model,
                expires_at=event.expires_at,
                created_at=state.built_at,
            )
        except Exception as error:
            if (
                isinstance(error, BudgetDenied)
                and error.reason_code in TERMINAL_COMPLETION_REJECTIONS
            ):
                raise
            return await self._fallback.deliberate(event, state, attention)
