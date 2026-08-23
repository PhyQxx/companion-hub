from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from .service import DeviceRegistry, DeviceSnapshot

GENERIC_DESKTOP_TARGETS = frozenset(
    {
        "computer",
        "my computer",
        "这台电脑",
        "我的电脑",
        "电脑",
    }
)


@dataclass(frozen=True, slots=True)
class DeviceTargetCandidate:
    device_id: UUID
    name: str
    alias: str | None
    client_type: str


class DeviceTargetResolutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        candidates: tuple[DeviceTargetCandidate, ...] = (),
    ) -> None:
        super().__init__(message)
        self.candidates = candidates


class DeviceTargetNotFound(DeviceTargetResolutionError):
    pass


class DeviceTargetUnavailable(DeviceTargetResolutionError):
    pass


class DeviceTargetAmbiguous(DeviceTargetResolutionError):
    pass


class DeviceTargetResolver:
    """Resolve user-facing device references without silently changing targets."""

    def __init__(self, registry: DeviceRegistry) -> None:
        self._registry = registry

    async def resolve(
        self,
        *,
        owner_user_id: UUID,
        target: str | UUID | None,
        capability: str,
    ) -> DeviceSnapshot:
        devices = [
            device
            for device in await self._registry.list_devices(owner_user_id=owner_user_id)
            if device.revoked_at is None
        ]
        matched = self._match_explicit(devices, target)
        if matched is not None:
            return self._select_available(matched, capability=capability, explicit=True)

        normalized = _normalize_target(target)
        if normalized and normalized not in GENERIC_DESKTOP_TARGETS:
            raise DeviceTargetNotFound("device target was not found")

        desktop_devices = [device for device in devices if device.client_type == "desktop"]
        return self._select_available(
            desktop_devices,
            capability=capability,
            explicit=False,
        )

    @staticmethod
    def _match_explicit(
        devices: list[DeviceSnapshot], target: str | UUID | None
    ) -> list[DeviceSnapshot] | None:
        if isinstance(target, UUID):
            return [device for device in devices if device.id == target]
        normalized = _normalize_target(target)
        if not normalized:
            return None
        try:
            target_id = UUID(normalized)
        except ValueError:
            target_id = None
        if target_id is not None:
            return [device for device in devices if device.id == target_id]
        alias_matches = [device for device in devices if device.alias == normalized]
        if alias_matches:
            return alias_matches
        name_matches = [device for device in devices if device.name.casefold() == normalized]
        return name_matches or None

    @staticmethod
    def _select_available(
        devices: list[DeviceSnapshot], *, capability: str, explicit: bool
    ) -> DeviceSnapshot:
        available = [
            device
            for device in devices
            if device.online and capability in device.effective_capabilities
        ]
        if len(available) == 1:
            return available[0]
        candidates = tuple(_candidate(device) for device in (available or devices))
        if len(available) > 1:
            raise DeviceTargetAmbiguous(
                "multiple devices match the target; user confirmation is required",
                candidates=candidates,
            )
        if devices:
            raise DeviceTargetUnavailable(
                "matched device is offline or lacks the required capability"
                if explicit
                else "no online desktop has the required capability",
                candidates=candidates,
            )
        raise DeviceTargetNotFound("device target was not found")


def _normalize_target(target: str | UUID | None) -> str:
    return str(target).strip().casefold() if target is not None else ""


def _candidate(device: DeviceSnapshot) -> DeviceTargetCandidate:
    return DeviceTargetCandidate(
        device_id=device.id,
        name=device.name,
        alias=device.alias,
        client_type=device.client_type,
    )
