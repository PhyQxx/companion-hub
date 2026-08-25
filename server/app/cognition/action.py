from __future__ import annotations

from .models import ActionResult, CognitiveDecision, DecisionKind


class ActionEngine:
    """V1 safety boundary: autonomous writes are deliberately disabled."""

    async def execute(self, decision: CognitiveDecision) -> ActionResult:
        if decision.decision != DecisionKind.ACT:
            return ActionResult(
                decision_id=decision.id,
                outcome="not_applicable",
                reason_code="no_action_requested",
                verified=True,
            )
        return ActionResult(
            decision_id=decision.id,
            outcome="blocked",
            reason_code="autonomous_action_disabled",
            verified=True,
        )
