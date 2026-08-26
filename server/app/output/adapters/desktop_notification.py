from __future__ import annotations

from app.devices import DeviceTargetResolutionError, DeviceTargetResolver
from app.output.adapter import DeliveryIntent, DeliveryReceipt, OutputAdapter
from app.output.protocols import DesktopCommandGateway


class DesktopNotificationAdapter(OutputAdapter):
    """通过设备命令通道向桌面客户端发送系统通知。"""

    def __init__(
        self,
        resolver: DeviceTargetResolver,
        gateway: DesktopCommandGateway,
    ) -> None:
        self._resolver = resolver
        self._gateway = gateway

    @property
    def name(self) -> str:
        return "desktop_notification"

    @property
    def available(self) -> bool:
        return self._resolver is not None and self._gateway is not None

    async def deliver(self, intent: DeliveryIntent) -> DeliveryReceipt:
        try:
            device = await self._resolver.resolve(
                owner_user_id=intent.user_id,
                target=None,
                capability="notification.show",
            )
        except DeviceTargetResolutionError:
            return DeliveryReceipt(
                delivered=False,
                channel=self.name,
                reason_code="desktop_channel_unavailable",
            )
        key = f"proactive:{intent.decision_id or intent.trigger_kind}:{device.id}"
        command = await self._gateway.issue(
            device_id=device.id,
            command="notification.show",
            args={
                "title": "Aria",
                "body": intent.text,
                "privacy_level": str(intent.privacy_level),
            },
            idempotency_key=key,
            ttl_seconds=8,
        )
        terminal = await self._gateway.wait_for_terminal(
            command.id,
            timeout_seconds=9,
        )
        delivered = terminal.status == "succeeded"
        return DeliveryReceipt(
            delivered=delivered,
            channel=self.name,
            reason_code=None if delivered else terminal.reason_code or terminal.status,
            external_operation_id=str(command.id),
        )
