"""Versioned facade delegating egress and existing application policy decisions."""

from app.schemas.common import PrivacyLevel
from app.schemas.policy import PolicyCapability, PolicyContext, PolicyDecision, PolicyPhase

from .egress import EgressBlocked, EgressDestination, EgressGuard


class PolicyService:
    def __init__(self, *, egress: EgressGuard | None = None) -> None:
        self._egress = egress or EgressGuard()

    def evaluate(
        self, context: PolicyContext, capability: PolicyCapability, phase: PolicyPhase
    ) -> PolicyDecision:
        if capability.prohibited:
            return PolicyDecision(action="deny", phase=phase, reason_code="capability_prohibited")
        try:
            self._egress.authorize(
                context.privacy_level,
                EgressDestination(
                    name=capability.name,
                    runs_local=capability.runs_local,
                    max_privacy_level=capability.max_privacy_level,
                ),
            )
        except EgressBlocked as error:
            return PolicyDecision(action="deny", phase=phase, reason_code=error.reason_code)
        if capability.requires_confirmation and not context.confirmed:
            return PolicyDecision(
                action="require_confirmation", phase=phase, reason_code="confirmation_required"
            )
        return PolicyDecision(
            action="local_only" if str(context.privacy_level) == "L2" else "allow", phase=phase
        )

    def authorize(
        self, privacy_level: str, destination: EgressDestination, *, phase: PolicyPhase
    ) -> PolicyDecision:
        decision = self.evaluate(
            PolicyContext(privacy_level=PrivacyLevel(privacy_level)),
            PolicyCapability(
                name=destination.name,
                runs_local=destination.runs_local,
                max_privacy_level=destination.max_privacy_level,
            ),
            phase,
        )
        if decision.action == "deny":
            raise EgressBlocked(decision.reason_code or "policy_denied")
        return decision

    @staticmethod
    def rejection(reason: str | None, *, phase: PolicyPhase) -> PolicyDecision:
        """Wrap a domain authority's decision without changing its rules."""
        return PolicyDecision(action="deny" if reason else "allow", reason_code=reason, phase=phase)

    @staticmethod
    def confirmation(policy: str, *, preauthorized: bool) -> PolicyDecision:
        if policy == "prohibited":
            return PolicyDecision(
                action="deny", phase="action", reason_code="capability_prohibited"
            )
        required = policy == "always" or (policy == "preauthorized" and not preauthorized)
        return PolicyDecision(
            action="require_confirmation" if required else "allow",
            phase="action",
            reason_code="confirmation_required" if required else None,
        )
