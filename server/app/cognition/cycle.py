from __future__ import annotations

from datetime import UTC, datetime

from app.ids import uuid7
from app.schemas import PrivacyLevel

from .attention import ATTENTION_POLICY_VERSION, AttentionEngine
from .deliberation import COGNITIVE_POLICY_VERSION, Deliberator
from .models import CognitiveDecision, DecisionKind, SemanticEvent, Urgency
from .store import CognitiveStore
from .world import WorldStateBuilder


class CognitiveCycle:
    def __init__(
        self,
        store: CognitiveStore,
        world: WorldStateBuilder,
        attention: AttentionEngine,
        deliberator: Deliberator,
    ) -> None:
        self.store = store
        self.world = world
        self.attention = attention
        self.deliberator = deliberator

    async def evaluate(self, event: SemanticEvent) -> CognitiveDecision:
        state = await self.world.build(event)
        attention = self.attention.evaluate(event, state)
        if not attention.should_deliberate:
            decision = CognitiveDecision(
                id=uuid7(),
                event_id=event.event_id,
                user_id=event.user_id,
                conversation_id=event.conversation_id,
                trigger_kind=event.kind,
                decision=DecisionKind.IGNORE,
                reason_codes=[*attention.reason_codes, "below_attention_threshold"],
                evidence_ids=event.evidence_ids,
                confidence=event.confidence,
                urgency=Urgency.LOW,
                attention_score=attention.score,
                policy_version=f"{ATTENTION_POLICY_VERSION}+{COGNITIVE_POLICY_VERSION}",
                expires_at=event.expires_at,
                created_at=datetime.now(UTC),
            )
        else:
            decision = await self.deliberator.deliberate(event, state, attention)
        if event.privacy_level != PrivacyLevel.L3:
            await self.store.save_decision(decision)
        return decision

    async def suppress(self, event: SemanticEvent, *reason_codes: str) -> CognitiveDecision:
        decision = CognitiveDecision(
            id=uuid7(),
            event_id=event.event_id,
            user_id=event.user_id,
            conversation_id=event.conversation_id,
            trigger_kind=event.kind,
            decision=DecisionKind.IGNORE,
            reason_codes=list(reason_codes),
            evidence_ids=event.evidence_ids,
            confidence=event.confidence,
            urgency=Urgency.LOW,
            attention_score=0,
            policy_version=f"{ATTENTION_POLICY_VERSION}+proactive-policy-v1",
            expires_at=event.expires_at,
            created_at=datetime.now(UTC),
        )
        if event.privacy_level != PrivacyLevel.L3:
            await self.store.save_decision(decision)
        return decision
