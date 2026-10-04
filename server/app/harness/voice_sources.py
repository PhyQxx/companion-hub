"""Content-free voice source authority; providers and SQL stay behind the port."""

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from app.schemas import PrivacyLevel


@dataclass(frozen=True, slots=True)
class VoiceSourceClaim:
    user_id: UUID
    conversation_id: UUID | None
    actor: Literal["browser", "satellite"]
    actor_id: UUID
    privacy_level: PrivacyLevel


@dataclass(frozen=True, slots=True)
class VoiceRecipientClaim:
    user_id: UUID
    device_id: UUID
    capability: Literal["avatar.chat", "voice.satellite"]
    privacy_level: PrivacyLevel


VoiceAuthority = VoiceSourceClaim | VoiceRecipientClaim


class VoiceSourceGuard(Protocol):
    async def validate(self, source: VoiceAuthority) -> None: ...


@dataclass(frozen=True, slots=True)
class VoiceRunFence:
    run_id: UUID
    budget_enabled: bool
    check_lineage: bool = False
    parent_run_id: UUID | None = None
    conversation_id: UUID | None = None
    quota_scope: dict[str, object] | None = None
    allow_succeeded: bool = False
    check_source: bool = False
    source_actor: str | None = None
    source_id: UUID | None = None


class RootVoiceSourceGuard(VoiceSourceGuard, Protocol):
    async def validate_live_runs(
        self, source: VoiceAuthority, fences: tuple[VoiceRunFence, ...]
    ) -> None: ...
