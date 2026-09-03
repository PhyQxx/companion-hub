from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from app.cognition import CognitiveDecision
from app.config import ConfigStore, DatabaseConfigStore, ProactiveChannelConfig
from app.db import AppUserRecord, Database, ProactiveDeliveryReceiptRecord
from app.devices import DeviceTargetResolutionError, DeviceTargetResolver
from app.ids import uuid7
from app.output.adapter import DeliveryIntent, OutputAdapter
from app.output.adapters import DesktopNotificationAdapter, VoiceAdapter, WebChatAdapter
from app.output.protocols import (
    ChatProactiveBroadcaster as ChatProactiveBroadcaster,
)
from app.output.protocols import (
    ChatProactiveStore as ChatProactiveStore,
)
from app.output.protocols import (
    DesktopCommandGateway as DesktopCommandGateway,
)
from app.output.protocols import (
    VoiceProactiveBroadcaster as VoiceProactiveBroadcaster,
)
from app.schemas import PrivacyLevel

_PRIVACY_RANK = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}
_CRITICAL_KINDS = {"water_leak", "water_leak_detected", "safety.alarm"}


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


class ProactiveDeliveryService:
    def __init__(
        self,
        database: Database,
        config_store: ConfigStore | DatabaseConfigStore,
        chat: ChatProactiveStore,
        chat_broadcaster: ChatProactiveBroadcaster,
        *,
        device_resolver: DeviceTargetResolver | None = None,
        device_gateway: DesktopCommandGateway | None = None,
        voice_broadcaster: VoiceProactiveBroadcaster | None = None,
        push_adapter: OutputAdapter | None = None,
        adapters: list[OutputAdapter] | None = None,
    ) -> None:
        self._database = database
        self._config_store = config_store
        self._chat = chat
        self._chat_broadcaster = chat_broadcaster
        self._device_resolver = device_resolver
        self._device_gateway = device_gateway
        self._voice_broadcaster = voice_broadcaster
        # 内部构建适配器列表；外部也可直接传入自定义适配器
        self._adapters: list[OutputAdapter] = list(adapters) if adapters is not None else []
        if not self._adapters:
            self._adapters.append(WebChatAdapter(chat, chat_broadcaster))
            if device_resolver is not None and device_gateway is not None:
                self._adapters.append(
                    DesktopNotificationAdapter(device_resolver, device_gateway)
                )
            if push_adapter is not None:
                self._adapters.append(push_adapter)
            if voice_broadcaster is not None:
                self._adapters.append(VoiceAdapter(voice_broadcaster))

    async def deliver(
        self,
        text: str,
        *,
        entity_id: str,
        rule_id: str,
        trigger_kind: str,
        privacy_level: PrivacyLevel,
        cognitive_decision: CognitiveDecision | None = None,
        target_user_id: UUID | None = None,
    ) -> ProactiveDeliveryResult | None:
        snapshot = (
            await self._config_store.refresh()
            if isinstance(self._config_store, DatabaseConfigStore)
            else self._config_store.current
        )
        config = snapshot.config.proactive_output
        if not config.enabled:
            return None
        user_id = target_user_id or (
            cognitive_decision.user_id if cognitive_decision is not None else None
        )
        if user_id is None:
            user_id = await self._active_user_id()
        if user_id is None:
            return None
        channels = sorted(
            (
                ("web_chat", config.web_chat),
                ("desktop_notification", config.desktop_notification),
                ("web_push", config.web_push),
                ("voice", config.voice),
            ),
            key=lambda item: item[1].priority,
            reverse=True,
        )
        attempts: list[ProactiveChannelAttempt] = []
        conversation_id: UUID | None = None
        # 按配置优先级排序适配器
        adapter_map = {a.name: a for a in self._adapters}
        for channel, policy in channels:
            if not self._eligible(policy, privacy_level, trigger_kind):
                continue
            adapter = adapter_map.get(channel)
            if adapter is None or not adapter.available:
                continue
            intent = DeliveryIntent(
                user_id=user_id,
                text=text,
                privacy_level=privacy_level,
                trigger_kind=trigger_kind,
                entity_id=entity_id,
                rule_id=rule_id,
                decision_id=cognitive_decision.id if cognitive_decision else None,
            )
            receipt = await adapter.deliver(intent)
            attempt = ProactiveChannelAttempt(
                channel=receipt.channel,
                delivered=receipt.delivered,
                reason_code=receipt.reason_code,
                external_operation_id=receipt.external_operation_id,
            )
            attempts.append(attempt)
            if (
                channel == "web_chat"
                and receipt.delivered
                and receipt.metadata is not None
                and "conversation_id" in receipt.metadata
            ):
                from uuid import UUID as _UUID

                conversation_id = _UUID(str(receipt.metadata["conversation_id"]))
            if attempt.delivered and config.delivery_mode == "first_available":
                break
        await self._record_attempts(
            user_id,
            attempts,
            privacy_level=privacy_level,
            decision_id=cognitive_decision.id if cognitive_decision else None,
        )
        if not any(item.delivered for item in attempts):
            return None
        return ProactiveDeliveryResult(
            user_id=user_id,
            conversation_id=conversation_id,
            attempts=tuple(attempts),
        )

    async def _web(
        self,
        user_id: UUID,
        text: str,
        *,
        entity_id: str,
        rule_id: str,
        trigger_kind: str,
        privacy_level: PrivacyLevel,
        cognitive_decision: CognitiveDecision | None,
    ) -> tuple[ProactiveChannelAttempt, UUID | None]:
        result = await self._chat.create_proactive_message(
            text,
            entity_id=entity_id,
            rule_id=rule_id,
            trigger_kind=trigger_kind,
            privacy_level=privacy_level,
            cognitive_decision=cognitive_decision,
            target_user_id=user_id,
        )
        if result is None:
            return ProactiveChannelAttempt("web_chat", False, "no_active_conversation"), None
        _, message = result
        await self._chat_broadcaster.broadcast_proactive(user_id, message)
        return (
            ProactiveChannelAttempt(
                "web_chat", True, external_operation_id=str(message.id)
            ),
            message.conversation_id,
        )

    async def _desktop(
        self,
        user_id: UUID,
        text: str,
        *,
        trigger_kind: str,
        privacy_level: PrivacyLevel,
        decision_id: UUID | None,
    ) -> ProactiveChannelAttempt:
        if self._device_resolver is None or self._device_gateway is None:
            return ProactiveChannelAttempt(
                "desktop_notification", False, "desktop_channel_unavailable"
            )
        try:
            device = await self._device_resolver.resolve(
                owner_user_id=user_id,
                target=None,
                capability="notification.show",
            )
        except DeviceTargetResolutionError:
            return ProactiveChannelAttempt(
                "desktop_notification", False, "desktop_channel_unavailable"
            )
        key = f"proactive:{decision_id or trigger_kind}:{device.id}"
        command = await self._device_gateway.issue(
            device_id=device.id,
            command="notification.show",
            args={
                "title": "Aria",
                "body": text,
                "privacy_level": str(privacy_level),
            },
            idempotency_key=key,
            ttl_seconds=8,
        )
        terminal = await self._device_gateway.wait_for_terminal(
            command.id,
            timeout_seconds=9,
        )
        delivered = terminal.status == "succeeded"
        return ProactiveChannelAttempt(
            "desktop_notification",
            delivered,
            None if delivered else terminal.reason_code or terminal.status,
            str(command.id),
        )

    async def _voice(
        self,
        user_id: UUID,
        text: str,
        *,
        privacy_level: PrivacyLevel,
    ) -> ProactiveChannelAttempt:
        if self._voice_broadcaster is None:
            return ProactiveChannelAttempt("voice", False, "voice_channel_unavailable")
        delivered = await self._voice_broadcaster.broadcast_proactive(
            user_id,
            text,
            privacy_level=privacy_level,
        )
        return ProactiveChannelAttempt(
            "voice",
            delivered > 0,
            None if delivered > 0 else "no_idle_voice_session",
        )

    async def _active_user_id(self) -> UUID | None:
        async with self._database.sessions() as session:
            value = await session.scalar(
                select(AppUserRecord.id)
                .where(AppUserRecord.status == "active")
                .order_by(AppUserRecord.created_at)
                .limit(1)
            )
        return value if isinstance(value, UUID) else None

    async def _record_attempts(
        self,
        user_id: UUID,
        attempts: list[ProactiveChannelAttempt],
        *,
        privacy_level: PrivacyLevel,
        decision_id: UUID | None,
    ) -> None:
        if not attempts:
            return
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            session.add_all(
                ProactiveDeliveryReceiptRecord(
                    id=uuid7(),
                    user_id=user_id,
                    decision_id=decision_id,
                    channel=attempt.channel,
                    status="delivered" if attempt.delivered else "failed",
                    reason_code=attempt.reason_code,
                    external_operation_id=attempt.external_operation_id,
                    privacy_level=str(privacy_level),
                    created_at=now,
                )
                for attempt in attempts
            )

    @staticmethod
    def _eligible(
        policy: ProactiveChannelConfig,
        privacy_level: PrivacyLevel,
        trigger_kind: str,
    ) -> bool:
        return (
            policy.enabled
            and _PRIVACY_RANK[str(privacy_level)] <= _PRIVACY_RANK[policy.max_privacy_level]
            and (not policy.critical_only or trigger_kind in _CRITICAL_KINDS)
        )
