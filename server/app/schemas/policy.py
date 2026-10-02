"""Content-free decisions; confirmations are supplied by trusted application adapters."""

from typing import Literal

from .common import PrivacyLevel, StrictModel

PolicyPhase = Literal["intake", "context", "model", "tool", "action", "delivery"]


class PolicyContext(StrictModel):
    privacy_level: PrivacyLevel
    confirmed: bool = False


class PolicyCapability(StrictModel):
    name: str
    runs_local: bool
    max_privacy_level: PrivacyLevel
    requires_confirmation: bool = False
    prohibited: bool = False


class PolicyDecision(StrictModel):
    action: Literal["allow", "deny", "require_confirmation", "local_only", "redact", "degrade"]
    phase: PolicyPhase
    reason_code: str | None = None
    policy_version: str = "policy-v1"
