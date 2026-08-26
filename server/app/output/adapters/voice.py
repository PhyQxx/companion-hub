from __future__ import annotations

from app.output.adapter import DeliveryIntent, DeliveryReceipt, OutputAdapter
from app.output.protocols import VoiceProactiveBroadcaster


class VoiceAdapter(OutputAdapter):
    """向在线空闲语音会话投递主动消息。"""

    def __init__(self, broadcaster: VoiceProactiveBroadcaster) -> None:
        self._broadcaster = broadcaster

    @property
    def name(self) -> str:
        return "voice"

    @property
    def available(self) -> bool:
        return self._broadcaster is not None

    async def deliver(self, intent: DeliveryIntent) -> DeliveryReceipt:
        count = await self._broadcaster.broadcast_proactive(
            intent.user_id,
            intent.text,
            privacy_level=intent.privacy_level,
        )
        delivered = count > 0
        return DeliveryReceipt(
            delivered=delivered,
            channel=self.name,
            reason_code=None if delivered else "no_idle_voice_session",
        )
