from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.schemas import PrivacyLevel


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    delivered: bool
    channel: str
    reason_code: str | None = None
    external_operation_id: str | None = None
    metadata: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class DeliveryIntent:
    user_id: UUID
    text: str
    privacy_level: PrivacyLevel
    trigger_kind: str
    entity_id: str
    rule_id: str
    decision_id: UUID | None = None


class OutputAdapter(Protocol):
    """主动投递适配器协议。

    每个实现负责一个投递通道（Web 私聊、桌面通知、语音等）。
    """

    @property
    def name(self) -> str: ...

    @property
    def available(self) -> bool: ...

    async def deliver(self, intent: DeliveryIntent) -> DeliveryReceipt: ...
