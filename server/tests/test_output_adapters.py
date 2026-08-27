from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.devices import DeviceTargetResolutionError
from app.output.adapter import DeliveryIntent
from app.output.adapters import DesktopNotificationAdapter, VoiceAdapter, WebChatAdapter
from app.schemas import PrivacyLevel


@pytest.fixture
def intent() -> DeliveryIntent:
    return DeliveryIntent(
        user_id=UUID("0198b2f4-3b00-7001-8000-000000000001"),
        text="有人按门铃",
        privacy_level=PrivacyLevel.L1,
        trigger_kind="doorbell",
        entity_id="sensor.doorbell",
        rule_id="rule-001",
        decision_id=UUID("0198b2f4-3b00-7002-8000-000000000002"),
    )


class TestWebChatAdapter:
    async def test_deliver_success(self, intent: DeliveryIntent) -> None:
        store = AsyncMock()
        msg = SimpleNamespace(
            id=UUID("0198b2f4-3b00-7003-8000-000000000003"),
            conversation_id=UUID("0198b2f4-3b00-7004-8000-000000000004"),
        )
        store.create_proactive_message.return_value = (
            UUID("0198b2f4-3b00-7003-8000-000000000003"),
            msg,
        )
        broadcaster = AsyncMock()
        adapter = WebChatAdapter(store, broadcaster)

        receipt = await adapter.deliver(intent)

        assert receipt.delivered is True
        assert receipt.channel == "web_chat"
        assert receipt.external_operation_id == str(msg.id)
        store.create_proactive_message.assert_awaited_once()
        broadcaster.broadcast_proactive.assert_awaited_once()

    async def test_deliver_no_conversation(self, intent: DeliveryIntent) -> None:
        store = AsyncMock()
        store.create_proactive_message.return_value = None
        broadcaster = AsyncMock()
        adapter = WebChatAdapter(store, broadcaster)

        receipt = await adapter.deliver(intent)

        assert receipt.delivered is False
        assert receipt.reason_code == "no_active_conversation"
        broadcaster.broadcast_proactive.assert_not_awaited()

    def test_properties(self) -> None:
        adapter = WebChatAdapter(AsyncMock(), AsyncMock())
        assert adapter.name == "web_chat"
        assert adapter.available is True


class TestDesktopNotificationAdapter:
    async def test_deliver_success(self, intent: DeliveryIntent) -> None:
        device = SimpleNamespace(id=UUID("0198b2f4-3b00-7005-8000-000000000005"))
        resolver = AsyncMock()
        resolver.resolve.return_value = device
        command = SimpleNamespace(
            id=UUID("0198b2f4-3b00-7006-8000-000000000006"),
            status="succeeded",
            reason_code=None,
        )
        gateway = AsyncMock()
        gateway.issue.return_value = command
        gateway.wait_for_terminal.return_value = command
        adapter = DesktopNotificationAdapter(resolver, gateway)

        receipt = await adapter.deliver(intent)

        assert receipt.delivered is True
        assert receipt.channel == "desktop_notification"
        assert receipt.external_operation_id == str(command.id)
        resolver.resolve.assert_awaited_once_with(
            owner_user_id=intent.user_id,
            target=None,
            capability="notification.show",
        )

    async def test_deliver_resolution_fails(self, intent: DeliveryIntent) -> None:
        resolver = AsyncMock()
        resolver.resolve.side_effect = DeviceTargetResolutionError("no_device")
        gateway = AsyncMock()
        adapter = DesktopNotificationAdapter(resolver, gateway)

        receipt = await adapter.deliver(intent)

        assert receipt.delivered is False
        assert receipt.reason_code == "desktop_channel_unavailable"
        gateway.issue.assert_not_called()

    async def test_deliver_command_fails(self, intent: DeliveryIntent) -> None:
        device = SimpleNamespace(id=UUID("0198b2f4-3b00-7005-8000-000000000005"))
        resolver = AsyncMock()
        resolver.resolve.return_value = device
        command = SimpleNamespace(
            id=UUID("0198b2f4-3b00-7006-8000-000000000006"),
            status="failed",
            reason_code="device_offline",
        )
        gateway = AsyncMock()
        gateway.issue.return_value = command
        gateway.wait_for_terminal.return_value = command
        adapter = DesktopNotificationAdapter(resolver, gateway)

        receipt = await adapter.deliver(intent)

        assert receipt.delivered is False
        assert receipt.reason_code == "device_offline"

    def test_available(self) -> None:
        assert DesktopNotificationAdapter(AsyncMock(), AsyncMock()).available is True
        assert DesktopNotificationAdapter(None, None).available is False  # type: ignore[arg-type]

    def test_name(self) -> None:
        adapter = DesktopNotificationAdapter(AsyncMock(), AsyncMock())
        assert adapter.name == "desktop_notification"


class TestVoiceAdapter:
    async def test_deliver_success(self, intent: DeliveryIntent) -> None:
        broadcaster = AsyncMock()
        broadcaster.broadcast_proactive.return_value = 2
        adapter = VoiceAdapter(broadcaster)

        receipt = await adapter.deliver(intent)

        assert receipt.delivered is True
        assert receipt.channel == "voice"
        broadcaster.broadcast_proactive.assert_awaited_once_with(
            intent.user_id,
            intent.text,
            privacy_level=intent.privacy_level,
        )

    async def test_deliver_no_sessions(self, intent: DeliveryIntent) -> None:
        broadcaster = AsyncMock()
        broadcaster.broadcast_proactive.return_value = 0
        adapter = VoiceAdapter(broadcaster)

        receipt = await adapter.deliver(intent)

        assert receipt.delivered is False
        assert receipt.reason_code == "no_idle_voice_session"

    def test_available(self) -> None:
        assert VoiceAdapter(AsyncMock()).available is True
        assert VoiceAdapter(None).available is False  # type: ignore[arg-type]

    def test_name(self) -> None:
        assert VoiceAdapter(AsyncMock()).name == "voice"
