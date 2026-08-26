from __future__ import annotations

from app.output.adapter import DeliveryIntent, DeliveryReceipt, OutputAdapter
from app.output.protocols import ChatProactiveBroadcaster, ChatProactiveStore


class WebChatAdapter(OutputAdapter):
    """通过 WebSocket 向 Web 聊天会话投递主动消息。"""

    def __init__(
        self,
        store: ChatProactiveStore,
        broadcaster: ChatProactiveBroadcaster,
    ) -> None:
        self._store = store
        self._broadcaster = broadcaster

    @property
    def name(self) -> str:
        return "web_chat"

    @property
    def available(self) -> bool:
        return True

    async def deliver(self, intent: DeliveryIntent) -> DeliveryReceipt:
        result = await self._store.create_proactive_message(
            intent.text,
            entity_id=intent.entity_id,
            rule_id=intent.rule_id,
            trigger_kind=intent.trigger_kind,
            privacy_level=intent.privacy_level,
            target_user_id=intent.user_id,
        )
        if result is None:
            return DeliveryReceipt(
                delivered=False,
                channel=self.name,
                reason_code="no_active_conversation",
            )
        _message_id, message = result
        await self._broadcaster.broadcast_proactive(intent.user_id, message)
        meta: dict[str, object] = {}
        if message.conversation_id is not None:
            meta["conversation_id"] = str(message.conversation_id)
        return DeliveryReceipt(
            delivered=True,
            channel=self.name,
            external_operation_id=str(message.id),
            metadata=meta,
        )
