"""Public devices exports; implementations load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .assets import (
        DEVICE_ASSET_TTL,
        MAX_BROWSER_DOCUMENT_BYTES,
        MAX_DEVICE_ASSET_BYTES,
        EphemeralDeviceAsset,
        EphemeralDeviceAssetError,
        EphemeralDeviceAssetNotFound,
        EphemeralDeviceAssetStore,
    )
    from .commands import (
        TERMINAL_COMMAND_STATUSES,
        CommandSnapshot,
        DeviceCommandConflict,
        DeviceCommandError,
        DeviceCommandNotFound,
        DeviceCommandStore,
        IssuedCommand,
    )
    from .presence import MqttPresenceBridge
    from .resolver import (
        GENERIC_DESKTOP_TARGETS,
        DeviceTargetAmbiguous,
        DeviceTargetCandidate,
        DeviceTargetNotFound,
        DeviceTargetResolutionError,
        DeviceTargetResolver,
        DeviceTargetUnavailable,
    )
    from .service import (
        DEVICE_ONLINE_WINDOW,
        DeviceAliasConflict,
        DeviceCredentialInvalid,
        DeviceNotFound,
        DevicePrincipal,
        DeviceRegistry,
        DeviceRegistryError,
        DeviceRevisionConflict,
        DeviceSnapshot,
        PairedDevice,
        PairingCodeInvalid,
        PairingSecret,
    )

_EXPORTS = {
    "DEVICE_ASSET_TTL": ("app.devices.assets", "DEVICE_ASSET_TTL"),
    "DEVICE_ONLINE_WINDOW": ("app.devices.service", "DEVICE_ONLINE_WINDOW"),
    "GENERIC_DESKTOP_TARGETS": ("app.devices.resolver", "GENERIC_DESKTOP_TARGETS"),
    "MAX_BROWSER_DOCUMENT_BYTES": ("app.devices.assets", "MAX_BROWSER_DOCUMENT_BYTES"),
    "MAX_DEVICE_ASSET_BYTES": ("app.devices.assets", "MAX_DEVICE_ASSET_BYTES"),
    "TERMINAL_COMMAND_STATUSES": ("app.devices.commands", "TERMINAL_COMMAND_STATUSES"),
    "CommandSnapshot": ("app.devices.commands", "CommandSnapshot"),
    "DeviceAliasConflict": ("app.devices.service", "DeviceAliasConflict"),
    "DeviceCommandConflict": ("app.devices.commands", "DeviceCommandConflict"),
    "DeviceCommandError": ("app.devices.commands", "DeviceCommandError"),
    "DeviceCommandNotFound": ("app.devices.commands", "DeviceCommandNotFound"),
    "DeviceCommandStore": ("app.devices.commands", "DeviceCommandStore"),
    "DeviceCredentialInvalid": ("app.devices.service", "DeviceCredentialInvalid"),
    "DeviceNotFound": ("app.devices.service", "DeviceNotFound"),
    "DevicePrincipal": ("app.devices.service", "DevicePrincipal"),
    "DeviceRegistry": ("app.devices.service", "DeviceRegistry"),
    "DeviceRegistryError": ("app.devices.service", "DeviceRegistryError"),
    "DeviceRevisionConflict": ("app.devices.service", "DeviceRevisionConflict"),
    "DeviceSnapshot": ("app.devices.service", "DeviceSnapshot"),
    "DeviceTargetAmbiguous": ("app.devices.resolver", "DeviceTargetAmbiguous"),
    "DeviceTargetCandidate": ("app.devices.resolver", "DeviceTargetCandidate"),
    "DeviceTargetNotFound": ("app.devices.resolver", "DeviceTargetNotFound"),
    "DeviceTargetResolutionError": ("app.devices.resolver", "DeviceTargetResolutionError"),
    "DeviceTargetResolver": ("app.devices.resolver", "DeviceTargetResolver"),
    "DeviceTargetUnavailable": ("app.devices.resolver", "DeviceTargetUnavailable"),
    "EphemeralDeviceAsset": ("app.devices.assets", "EphemeralDeviceAsset"),
    "EphemeralDeviceAssetError": ("app.devices.assets", "EphemeralDeviceAssetError"),
    "EphemeralDeviceAssetNotFound": ("app.devices.assets", "EphemeralDeviceAssetNotFound"),
    "EphemeralDeviceAssetStore": ("app.devices.assets", "EphemeralDeviceAssetStore"),
    "IssuedCommand": ("app.devices.commands", "IssuedCommand"),
    "MqttPresenceBridge": ("app.devices.presence", "MqttPresenceBridge"),
    "PairedDevice": ("app.devices.service", "PairedDevice"),
    "PairingCodeInvalid": ("app.devices.service", "PairingCodeInvalid"),
    "PairingSecret": ("app.devices.service", "PairingSecret"),
}

__all__ = [
    "DEVICE_ASSET_TTL",
    "DEVICE_ONLINE_WINDOW",
    "GENERIC_DESKTOP_TARGETS",
    "MAX_BROWSER_DOCUMENT_BYTES",
    "MAX_DEVICE_ASSET_BYTES",
    "TERMINAL_COMMAND_STATUSES",
    "CommandSnapshot",
    "DeviceAliasConflict",
    "DeviceCommandConflict",
    "DeviceCommandError",
    "DeviceCommandNotFound",
    "DeviceCommandStore",
    "DeviceCredentialInvalid",
    "DeviceNotFound",
    "DevicePrincipal",
    "DeviceRegistry",
    "DeviceRegistryError",
    "DeviceRevisionConflict",
    "DeviceSnapshot",
    "DeviceTargetAmbiguous",
    "DeviceTargetCandidate",
    "DeviceTargetNotFound",
    "DeviceTargetResolutionError",
    "DeviceTargetResolver",
    "DeviceTargetUnavailable",
    "EphemeralDeviceAsset",
    "EphemeralDeviceAssetError",
    "EphemeralDeviceAssetNotFound",
    "EphemeralDeviceAssetStore",
    "IssuedCommand",
    "MqttPresenceBridge",
    "PairedDevice",
    "PairingCodeInvalid",
    "PairingSecret",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
