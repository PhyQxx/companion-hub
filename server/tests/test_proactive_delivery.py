from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import select

from app.chat import MessageView
from app.config import ConfigStore, ProactiveOutputConfig
from app.db import Base, Database, ProactiveDeliveryReceiptRecord, create_database
from app.ids import uuid7
from app.output import ProactiveDeliveryService
from app.output.proactive import (
    ChatProactiveBroadcaster,
    ChatProactiveStore,
    DesktopCommandGateway,
    VoiceProactiveBroadcaster,
)
from app.schemas import PrivacyLevel


class FakeChat:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create_proactive_message(self, text: str, **kwargs) -> tuple[UUID, MessageView]:  # type: ignore[no-untyped-def]
        self.calls.append({"text": text, **kwargs})
        user_id = cast(UUID, kwargs["target_user_id"])
        now = datetime.now(UTC)
        return user_id, MessageView(
            id=uuid7(),
            conversation_id=uuid7(),
            turn_id=uuid7(),
            seq=1,
            role="assistant",
            content=text,
            privacy_level=str(kwargs["privacy_level"]),
            generation_id=None,
            decision_meta=None,
            created_at=now,
        )


class FakeChatBroadcaster:
    def __init__(self) -> None:
        self.messages: list[MessageView] = []

    async def broadcast_proactive(self, user_id: UUID, message: MessageView) -> None:
        del user_id
        self.messages.append(message)


class FakeResolver:
    async def resolve(self, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        return SimpleNamespace(id=uuid7())


class FakeGateway:
    def __init__(self) -> None:
        self.commands: list[dict[str, object]] = []

    async def issue(self, **kwargs):  # type: ignore[no-untyped-def]
        self.commands.append(kwargs)
        return SimpleNamespace(id=uuid7(), status="sent", reason_code=None)

    async def wait_for_terminal(self, command_id: UUID, **kwargs):  # type: ignore[no-untyped-def]
        del command_id, kwargs
        return SimpleNamespace(id=uuid7(), status="succeeded", reason_code=None)


class FakeVoice:
    def __init__(self, delivered: int = 1) -> None:
        self.delivered = delivered
        self.messages: list[str] = []

    async def broadcast_proactive(
        self,
        user_id: UUID,
        text: str,
        *,
        privacy_level: PrivacyLevel,
    ) -> int:
        del user_id, privacy_level
        self.messages.append(text)
        return self.delivered


async def service(config: ProactiveOutputConfig) -> tuple[
    ProactiveDeliveryService,
    FakeChat,
    FakeChatBroadcaster,
    FakeGateway,
    FakeVoice,
    Database,
]:
    chat = FakeChat()
    broadcaster = FakeChatBroadcaster()
    gateway = FakeGateway()
    voice = FakeVoice()
    store = cast(
        ConfigStore,
        SimpleNamespace(current=SimpleNamespace(config=SimpleNamespace(proactive_output=config))),
    )
    database = create_database("sqlite+aiosqlite://")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    delivery = ProactiveDeliveryService(
        database,
        store,
        cast(ChatProactiveStore, chat),
        cast(ChatProactiveBroadcaster, broadcaster),
        device_resolver=cast(object, FakeResolver()),  # type: ignore[arg-type]
        device_gateway=cast(DesktopCommandGateway, gateway),
        voice_broadcaster=cast(VoiceProactiveBroadcaster, voice),
    )
    return delivery, chat, broadcaster, gateway, voice, database


async def test_all_enabled_delivers_web_desktop_and_voice() -> None:
    config = ProactiveOutputConfig.model_validate(
        {
            "delivery_mode": "all_enabled",
            "web_chat": {"enabled": True, "priority": 100},
            "desktop_notification": {"enabled": True, "priority": 80},
            "voice": {"enabled": True, "priority": 60},
        }
    )
    delivery, chat, broadcaster, gateway, voice, database = await service(config)
    user_id = uuid7()

    result = await delivery.deliver(
        "检测到厨房水浸，请立即检查。",
        entity_id="binary_sensor.kitchen_leak",
        rule_id="water_leak",
        trigger_kind="water_leak",
        privacy_level=PrivacyLevel.L1,
        target_user_id=user_id,
    )

    assert result is not None
    assert result.delivered_channels == ("web_chat", "desktop_notification", "voice")
    assert chat.calls and broadcaster.messages
    assert gateway.commands[0]["command"] == "notification.show"
    assert voice.messages == ["检测到厨房水浸，请立即检查。"]
    async with database.sessions() as session:
        receipts = list(
            await session.scalars(
                select(ProactiveDeliveryReceiptRecord).order_by(
                    ProactiveDeliveryReceiptRecord.created_at
                )
            )
        )
    assert [receipt.channel for receipt in receipts] == [
        "web_chat",
        "desktop_notification",
        "voice",
    ]
    assert all(receipt.status == "delivered" for receipt in receipts)


async def test_first_available_uses_highest_priority_real_channel() -> None:
    config = ProactiveOutputConfig.model_validate(
        {
            "delivery_mode": "first_available",
            "web_chat": {"enabled": True, "priority": 50},
            "desktop_notification": {"enabled": True, "priority": 100},
            "voice": {"enabled": True, "priority": 20},
        }
    )
    delivery, chat, _, gateway, voice, _ = await service(config)

    result = await delivery.deliver(
        "欢迎回家。",
        entity_id="person.owner",
        rule_id="arrival",
        trigger_kind="user_arrived_home",
        privacy_level=PrivacyLevel.L1,
        target_user_id=uuid7(),
    )

    assert result is not None
    assert result.delivered_channels == ("desktop_notification",)
    assert gateway.commands
    assert chat.calls == []
    assert voice.messages == []


async def test_channel_privacy_and_critical_only_are_enforced() -> None:
    config = ProactiveOutputConfig.model_validate(
        {
            "web_chat": {
                "enabled": True,
                "priority": 100,
                "max_privacy_level": "L2",
                "critical_only": True,
            },
            "desktop_notification": {
                "enabled": True,
                "priority": 80,
                "max_privacy_level": "L0",
            },
            "voice": {"enabled": False},
        }
    )
    delivery, chat, _, gateway, _, _ = await service(config)

    normal = await delivery.deliver(
        "普通私密提醒",
        entity_id="sensor.private",
        rule_id="private",
        trigger_kind="temperature_high",
        privacy_level=PrivacyLevel.L2,
        target_user_id=uuid7(),
    )
    critical = await delivery.deliver(
        "紧急私密提醒",
        entity_id="sensor.private_leak",
        rule_id="private_leak",
        trigger_kind="water_leak",
        privacy_level=PrivacyLevel.L2,
        target_user_id=uuid7(),
    )

    assert normal is None
    assert critical is not None
    assert critical.delivered_channels == ("web_chat",)
    assert len(chat.calls) == 1
    assert gateway.commands == []


def test_enabled_output_requires_at_least_one_channel() -> None:
    with pytest.raises(ValueError, match="at least one channel"):
        ProactiveOutputConfig.model_validate(
            {
                "enabled": True,
                "web_chat": {"enabled": False},
                "desktop_notification": {"enabled": False},
                "voice": {"enabled": False},
            }
        )


def test_desktop_notifications_reject_l2_content_policy() -> None:
    with pytest.raises(ValueError, match="desktop notifications cannot carry L2"):
        ProactiveOutputConfig.model_validate(
            {"desktop_notification": {"enabled": True, "max_privacy_level": "L2"}}
        )


async def test_broadcast_overrides_first_available_and_hits_all_channels() -> None:
    """SAFE critical：broadcast=True 时不再首达短路，所有可用通道都投递。"""
    config = ProactiveOutputConfig.model_validate(
        {
            "enabled": True,
            "delivery_mode": "first_available",
            "web_chat": {"enabled": True, "priority": 100},
            "desktop_notification": {"enabled": True, "priority": 80},
            "voice": {"enabled": True, "priority": 60},
        }
    )
    delivery, chat, _, gateway, voice, database = await service(config)
    try:
        result = await delivery.deliver(
            "【危急】烟雾告警",
            entity_id="binary_sensor.smoke",
            rule_id="smoke_detected",
            trigger_kind="smoke_detected",
            privacy_level=PrivacyLevel.L1,
            target_user_id=uuid7(),
            broadcast=True,
        )
        assert result is not None
        channels = {attempt.channel for attempt in result.attempts if attempt.delivered}
        assert channels == {"web_chat", "desktop_notification", "voice"}
        assert len(chat.calls) == 1
        assert len(gateway.commands) == 1
        assert voice.messages == ["【危急】烟雾告警"]
    finally:
        await database.close()
