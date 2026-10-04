"""Public connection exports; SDKs load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .bridge import HomeAssistantBridge, HomeAssistantGateway
    from .client import HomeAssistantClient
    from .directory import DeviceDirectory, DeviceDirectoryEntry, DeviceSearchPage
    from .manager import HomeAssistantManager
    from .models import (
        HomeAssistantError,
        HomeAssistantHealth,
        HomeAssistantLogEntry,
        HomeAssistantState,
        HomeAssistantStateChange,
        HomeAssistantStatus,
    )
    from .proactive import HomeAssistantProactiveEngine
    from .tools import (
        HomeControlArgs,
        HomeControlTool,
        HomeGetHistoryArgs,
        HomeGetHistoryTool,
        HomeGetStateArgs,
        HomeGetStateTool,
        SearchDevicesArgs,
        SearchDevicesTool,
    )

_EXPORTS = {
    "DeviceDirectory": ("app.home_assistant.directory", "DeviceDirectory"),
    "DeviceDirectoryEntry": ("app.home_assistant.directory", "DeviceDirectoryEntry"),
    "DeviceSearchPage": ("app.home_assistant.directory", "DeviceSearchPage"),
    "HomeAssistantBridge": ("app.home_assistant.bridge", "HomeAssistantBridge"),
    "HomeAssistantClient": ("app.home_assistant.client", "HomeAssistantClient"),
    "HomeAssistantError": ("app.home_assistant.models", "HomeAssistantError"),
    "HomeAssistantGateway": ("app.home_assistant.bridge", "HomeAssistantGateway"),
    "HomeAssistantHealth": ("app.home_assistant.models", "HomeAssistantHealth"),
    "HomeAssistantLogEntry": ("app.home_assistant.models", "HomeAssistantLogEntry"),
    "HomeAssistantManager": ("app.home_assistant.manager", "HomeAssistantManager"),
    "HomeAssistantProactiveEngine": (
        "app.home_assistant.proactive",
        "HomeAssistantProactiveEngine",
    ),
    "HomeAssistantState": ("app.home_assistant.models", "HomeAssistantState"),
    "HomeAssistantStateChange": ("app.home_assistant.models", "HomeAssistantStateChange"),
    "HomeAssistantStatus": ("app.home_assistant.models", "HomeAssistantStatus"),
    "HomeControlArgs": ("app.home_assistant.tools", "HomeControlArgs"),
    "HomeControlTool": ("app.home_assistant.tools", "HomeControlTool"),
    "HomeGetHistoryArgs": ("app.home_assistant.tools", "HomeGetHistoryArgs"),
    "HomeGetHistoryTool": ("app.home_assistant.tools", "HomeGetHistoryTool"),
    "HomeGetStateArgs": ("app.home_assistant.tools", "HomeGetStateArgs"),
    "HomeGetStateTool": ("app.home_assistant.tools", "HomeGetStateTool"),
    "SearchDevicesArgs": ("app.home_assistant.tools", "SearchDevicesArgs"),
    "SearchDevicesTool": ("app.home_assistant.tools", "SearchDevicesTool"),
}

__all__ = [
    "DeviceDirectory",
    "DeviceDirectoryEntry",
    "DeviceSearchPage",
    "HomeAssistantBridge",
    "HomeAssistantClient",
    "HomeAssistantError",
    "HomeAssistantGateway",
    "HomeAssistantHealth",
    "HomeAssistantLogEntry",
    "HomeAssistantManager",
    "HomeAssistantProactiveEngine",
    "HomeAssistantState",
    "HomeAssistantStateChange",
    "HomeAssistantStatus",
    "HomeControlArgs",
    "HomeControlTool",
    "HomeGetHistoryArgs",
    "HomeGetHistoryTool",
    "HomeGetStateArgs",
    "HomeGetStateTool",
    "SearchDevicesArgs",
    "SearchDevicesTool",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
