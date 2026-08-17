from __future__ import annotations

from dataclasses import dataclass

from app.schemas import PrivacyLevel

_PRIVACY_RANK = {PrivacyLevel.L0: 0, PrivacyLevel.L1: 1, PrivacyLevel.L2: 2, PrivacyLevel.L3: 3}


class EgressBlocked(PermissionError):
    """Payload-free rejection safe to expose in logs and metrics."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class EgressDestination:
    name: str
    runs_local: bool
    max_privacy_level: PrivacyLevel


class EgressGuard:
    def authorize(self, privacy_level: PrivacyLevel | str, destination: EgressDestination) -> None:
        level = PrivacyLevel(privacy_level)
        if level is PrivacyLevel.L3:
            raise EgressBlocked("l3_egress_blocked")
        if level is PrivacyLevel.L2 and not destination.runs_local:
            raise EgressBlocked("l2_requires_local_destination")
        if _PRIVACY_RANK[level] > _PRIVACY_RANK[destination.max_privacy_level]:
            raise EgressBlocked("destination_privacy_limit")
