"""Web Push 投递适配器：把主动投递意图推送到用户所有已登记浏览器。

隐私：通道级 max_privacy_level 由 ProactiveDeliveryService 按
`proactive_output.web_push` 配置过滤（配置校验禁止 L2），L2 文本不进入
系统通知。通知正文按通知习惯截断，tag 用 entity_id 做同事件去重，
点击回流由前端 Service Worker 聚焦/打开聊天完成。
"""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from app.config import ConfigStore, DatabaseConfigStore
from app.output.adapter import DeliveryIntent, DeliveryReceipt
from app.push.sender import PushSendResult, PushSendStatus, VapidCredentials, WebPushSender
from app.push.store import PushSubscriptionStore

logger = logging.getLogger("app.push.adapter")

NOTIFY_BODY_LIMIT = 120


class WebPushAdapter:
    name = "web_push"

    def __init__(
        self,
        store: PushSubscriptionStore,
        sender: WebPushSender | None = None,
        *,
        config_store: ConfigStore | DatabaseConfigStore,
    ) -> None:
        self._store = store
        self._sender = sender or WebPushSender()
        self._config_store = config_store

    def _credentials(self) -> VapidCredentials | None:
        from app.push.sender import resolve_vapid_credentials

        config = self._config_store.current.config.integrations.push
        return resolve_vapid_credentials(config)

    @property
    def available(self) -> bool:
        return self._credentials() is not None

    async def deliver(self, intent: DeliveryIntent) -> DeliveryReceipt:
        credentials = self._credentials()
        if credentials is None:
            return DeliveryReceipt(
                delivered=False,
                channel=self.name,
                reason_code="push_not_configured",
            )
        subscriptions = await self._store.list_for_user(intent.user_id)
        if not subscriptions:
            return DeliveryReceipt(
                delivered=False,
                channel=self.name,
                reason_code="no_push_subscription",
            )
        payload = json.dumps(
            {
                "title": "Aria",
                "body": _truncate(intent.text),
                "tag": f"aria:{intent.entity_id}",
                "data": {
                    "entity_id": intent.entity_id,
                    "trigger_kind": intent.trigger_kind,
                    "privacy_level": str(intent.privacy_level),
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")

        delivered = 0
        gone: list[UUID] = []
        last_reason: str | None = None
        for subscription in subscriptions:
            info = {
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
            }
            result: PushSendResult = await asyncio.to_thread(
                self._sender.send, info, payload, credentials
            )
            if result.status is PushSendStatus.DELIVERED:
                delivered += 1
                await self._store.mark_delivered(subscription.id)
            elif result.status is PushSendStatus.GONE:
                gone.append(subscription.id)
            else:
                await self._store.mark_failed(subscription.id)
                last_reason = result.reason or "push_send_failed"
        for subscription_id in gone:
            await self._store.delete(subscription_id)
        if delivered > 0:
            return DeliveryReceipt(
                delivered=True,
                channel=self.name,
                external_operation_id=None,
                metadata={"attempts": len(subscriptions), "delivered": delivered},
            )
        if gone and len(gone) == len(subscriptions):
            reason = "no_push_subscription_left"
        else:
            reason = last_reason or "push_send_failed"
        return DeliveryReceipt(delivered=False, channel=self.name, reason_code=reason)


def _truncate(text: str) -> str:
    if len(text) <= NOTIFY_BODY_LIMIT:
        return text
    return text[:NOTIFY_BODY_LIMIT] + "…"
