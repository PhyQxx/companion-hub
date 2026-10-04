"""Owned speech delivery and explicit provider quotes, without SQL or SDKs."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from app.harness.unit_costs import UnitCostQuote
from app.harness.voice_sources import VoiceAuthority
from app.schemas import PrivacyLevel

from .contracts import SpeechSynthesizer


@dataclass(frozen=True, slots=True)
class VoiceProviderBinding:
    endpoint: str
    quote: UnitCostQuote | None
    config_version: int | None = None
    authority_fingerprint: str | None = None


class VoicePricingSource(Protocol):
    def binding_for(self, provider: object) -> VoiceProviderBinding | None: ...

    def validate_provider(self, provider: object) -> None: ...


class SpeechDeliveryContext(Protocol):
    async def validate(self) -> None: ...

    def synthesize(
        self, provider: SpeechSynthesizer, text: str, privacy_level: PrivacyLevel
    ) -> AsyncIterator[bytes]: ...


class SpeechDelivery(Protocol):
    async def execute(
        self,
        source: VoiceAuthority,
        invoke: Callable[[SpeechDeliveryContext], Awaitable[bool]],
    ) -> bool: ...
