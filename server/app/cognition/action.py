from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID

from .models import (
    ActionLevel,
    ActionOutcome,
    ActionPlan,
    ActionResult,
    CognitiveDecision,
    DecisionKind,
)


class ActionEngine:
    """M3B action execution with tiered safety boundaries.

    V1 safety boundary: A2 (pre-authorized) and A3 (confirmed) actions are
    deliberately disabled.  Only A0 (observe) and A1 (prompt) are operational.
    """

    _DECISION_TO_LEVEL: ClassVar[dict[DecisionKind, ActionLevel]] = {
        DecisionKind.IGNORE: ActionLevel.A0_OBSERVE,
        DecisionKind.RECORD: ActionLevel.A0_OBSERVE,
        DecisionKind.INFORM: ActionLevel.A1_PROMPT,
        DecisionKind.ASK: ActionLevel.A1_PROMPT,
        DecisionKind.SUGGEST: ActionLevel.A1_PROMPT,
        DecisionKind.ESCALATE: ActionLevel.A1_PROMPT,
        DecisionKind.ACT: ActionLevel.A2_PREAUTHORIZED,
    }

    def plan(self, decision: CognitiveDecision) -> ActionPlan:
        """Map a cognitive decision to an executable action plan."""
        level = self._DECISION_TO_LEVEL.get(decision.decision, ActionLevel.A0_OBSERVE)
        return ActionPlan(
            decision_id=decision.id,
            level=level,
            decision_kind=decision.decision,
            approval_required=decision.approval_required,
            message=decision.message,
            ttl_seconds=300,
        )

    async def execute(self, decision: CognitiveDecision) -> ActionResult:
        """Execute or block the planned action.

        V1 behaviour:
        - A0 decisions are recorded as observed.
        - A1 decisions are marked prompted (actual delivery happens via
          ProactiveDeliveryService downstream).
        - A2/A3 (act) are hard-blocked regardless of payload.
        """
        plan = self.plan(decision)
        now = datetime.now(UTC)

        if plan.level == ActionLevel.A0_OBSERVE:
            return ActionResult(
                decision_id=decision.id,
                outcome=ActionOutcome.OBSERVED,
                reason_code="A0_record_only",
                verified=True,
            )

        if plan.level == ActionLevel.A1_PROMPT:
            return ActionResult(
                decision_id=decision.id,
                outcome=ActionOutcome.PROMPTED,
                reason_code="A1_delivered_via_output",
                verified=False,  # actual delivery verification is async
                observed_state={
                    "approval_required": plan.approval_required,
                    "message_length": len(plan.message or ""),
                    "planned_at": now.isoformat(),
                },
            )

        # A2 / A3 are disabled in v1
        return ActionResult(
            decision_id=decision.id,
            outcome=ActionOutcome.BLOCKED,
            reason_code="autonomous_action_disabled",
            verified=True,
            observed_state={
                "requested_level": str(plan.level),
                "blocked_at": now.isoformat(),
                "policy_version": "action-v1",
            },
        )

    async def record_outcome(
        self,
        decision_id: UUID,
        *,
        outcome: ActionOutcome,
        reason_code: str | None = None,
        verified: bool = False,
        observed_state: dict[str, Any] | None = None,
    ) -> ActionResult:
        """Record the verified outcome of an earlier action plan.

        Called by the delivery layer after proactive messages are actually
        dispatched (or fail) so that the audit trail is complete.
        """
        return ActionResult(
            decision_id=decision_id,
            outcome=outcome,
            reason_code=reason_code or "outcome_recorded",
            verified=verified,
            observed_state=observed_state or {},
        )
