from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import UUID

from app.schemas import EphemeralSignal, InputEnvelope, PrivacyLevel

_PRIVACY_RANK = {
    PrivacyLevel.L0: 0,
    PrivacyLevel.L1: 1,
    PrivacyLevel.L2: 2,
    PrivacyLevel.L3: 3,
}


class L3PersistenceBlocked(ValueError):
    """Raised without payload details when a durable input is classified as L3."""

    def __init__(self) -> None:
        super().__init__("durable input blocked by server privacy policy")


@dataclass(frozen=True, slots=True)
class PrivacyPolicyRegistry:
    """Server-authoritative minimum privacy levels for input sources and channels."""

    default_level: PrivacyLevel = PrivacyLevel.L0
    adapter_levels: Mapping[UUID, PrivacyLevel] = field(default_factory=dict)
    content_levels: Mapping[str, PrivacyLevel] = field(default_factory=dict)
    channel_levels: Mapping[str, PrivacyLevel] = field(default_factory=dict)

    def _highest(self, *levels: PrivacyLevel | str) -> PrivacyLevel:
        normalized = (PrivacyLevel(level) for level in levels)
        return max(normalized, key=_PRIVACY_RANK.__getitem__)

    def classify_durable(self, event: InputEnvelope) -> InputEnvelope:
        content_levels = [
            self.content_levels.get(part.type, self.default_level) for part in event.content
        ]
        required = self._highest(
            PrivacyLevel(event.privacy_level),
            self.adapter_levels.get(event.source.adapter_instance_id, self.default_level),
            *content_levels,
        )
        if required is PrivacyLevel.L3:
            raise L3PersistenceBlocked
        if required == event.privacy_level:
            return event
        return InputEnvelope.model_validate(
            {**event.model_dump(mode="python"), "privacy_level": required}
        )

    def classify_ephemeral(self, signal: EphemeralSignal) -> EphemeralSignal:
        required = self._highest(
            PrivacyLevel(signal.privacy_level),
            self.adapter_levels.get(signal.source.adapter_instance_id, self.default_level),
            self.content_levels.get(signal.content.type, self.default_level),
            self.channel_levels.get(signal.channel, self.default_level),
        )
        if required == signal.privacy_level:
            return signal
        return EphemeralSignal.model_validate(
            {**signal.model_dump(mode="python"), "privacy_level": required}
        )
