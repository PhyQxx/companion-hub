"""Application-facing cognition ports; no persistence or provider implementations."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .models import AttentionResult, CognitiveDecision, SemanticEvent, WorldState

COGNITIVE_POLICY_VERSION = "cognitive-v1"


class DecisionRepository(Protocol):
    async def save_decision(
        self,
        decision: CognitiveDecision,
        *,
        event: SemanticEvent | None = None,
        state: WorldState | None = None,
        proactive_limit: int | None = None,
    ) -> CognitiveDecision | None: ...


class WorldStateSource(Protocol):
    async def build(self, event: SemanticEvent, *, now: datetime | None = None) -> WorldState: ...


class Deliberator(Protocol):
    async def deliberate(
        self, event: SemanticEvent, state: WorldState, attention: AttentionResult
    ) -> CognitiveDecision: ...


class CognitiveCyclePort(Protocol):
    async def evaluate(
        self, event: SemanticEvent, *, proactive_limit: int | None = None
    ) -> CognitiveDecision: ...

    async def suppress(self, event: SemanticEvent, *reason_codes: str) -> CognitiveDecision: ...
