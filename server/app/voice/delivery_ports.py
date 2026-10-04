"""Owned speech delivery and explicit provider quotes, without SQL or SDKs."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.harness.unit_costs import UnitCostQuote
from app.harness.voice_sources import VoiceAuthority, VoiceSourceClaim
from app.schemas import PrivacyLevel

from .contracts import SpeechRecognizer, SpeechSynthesizer


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


class RecognitionRequest(Protocol):
    async def feed(self, pcm: bytes) -> str | None: ...

    async def finalize(self) -> str | None: ...

    async def aclose(self) -> None: ...


class VoiceTurnContext(SpeechDeliveryContext, Protocol):
    run_id: UUID
    deadline: datetime

    def bind(self) -> AbstractContextManager[None]: ...

    async def transcribe(
        self, provider: SpeechRecognizer, pcm: bytes, *, sample_rate: int, language: str | None
    ) -> str: ...

    def recognition_request(
        self,
        provider: SpeechRecognizer,
        feed: Callable[[bytes], Awaitable[str | None]],
        finalize: Callable[[], Awaitable[str | None]],
    ) -> RecognitionRequest: ...

    async def finish(self, status: str, reason: str) -> None: ...


class VoiceTurnDelivery(Protocol):
    def validate_ephemeral(self) -> None: ...

    async def start(self, source: VoiceSourceClaim) -> VoiceTurnContext: ...
