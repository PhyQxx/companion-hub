from __future__ import annotations

from typing import Protocol
from uuid import UUID

from pydantic import JsonValue

from app.chat import MessageView
from app.cognition import CognitiveDecision
from app.schemas import PrivacyLevel


class ChatProactiveStore(Protocol):
    async def create_proactive_message(
        self,
        text: str,
        *,
        entity_id: str,
        rule_id: str,
        trigger_kind: str,
        privacy_level: PrivacyLevel,
        cognitive_decision: CognitiveDecision | None = None,
        target_user_id: UUID | None = None,
    ) -> tuple[UUID, MessageView] | None: ...


class ChatProactiveBroadcaster(Protocol):
    async def broadcast_proactive(self, user_id: UUID, message: MessageView) -> None: ...


class DesktopCommand(Protocol):
    id: UUID
    status: str
    reason_code: str | None


class DesktopCommandGateway(Protocol):
    async def issue(
        self,
        *,
        device_id: UUID,
        command: str,
        args: dict[str, JsonValue],
        idempotency_key: str,
        ttl_seconds: int,
    ) -> DesktopCommand: ...

    async def wait_for_terminal(
        self,
        command_id: UUID,
        *,
        timeout_seconds: float | None = None,
    ) -> DesktopCommand: ...


class VoiceProactiveBroadcaster(Protocol):
    async def broadcast_proactive(
        self,
        user_id: UUID,
        text: str,
        *,
        privacy_level: PrivacyLevel,
    ) -> int: ...
