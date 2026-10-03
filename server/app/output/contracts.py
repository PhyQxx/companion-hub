"""Detached proactive output facts and persistence port."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.schemas import PrivacyLevel


@dataclass(frozen=True, slots=True)
class ProactiveChannelAttempt:
    channel: str
    delivered: bool
    reason_code: str | None = None
    external_operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProactiveDeliveryResult:
    user_id: UUID
    conversation_id: UUID | None
    attempts: tuple[ProactiveChannelAttempt, ...]

    @property
    def delivered_channels(self) -> tuple[str, ...]:
        return tuple(item.channel for item in self.attempts if item.delivered)


class ProactiveOutputRepository(Protocol):
    async def default_owner(self) -> UUID | None: ...

    async def owner_active(self, user_id: UUID) -> bool: ...

    async def record_attempts(
        self,
        user_id: UUID,
        attempts: tuple[ProactiveChannelAttempt, ...],
        *,
        privacy_level: PrivacyLevel,
        decision_id: UUID | None,
    ) -> None: ...
