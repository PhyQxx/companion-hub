"""Public connection exports; SDKs load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .client import SdkMcpClient, sdk_client_factory
    from .manager import McpManager, McpManagerError
    from .models import (
        McpCallPayload,
        McpCallResult,
        McpRemoteTool,
        McpServerState,
        McpToolDescriptor,
    )
    from .ports import McpClientFactory, McpRemoteClient

_EXPORTS = {
    "McpCallPayload": ("app.integrations.mcp.models", "McpCallPayload"),
    "McpCallResult": ("app.integrations.mcp.models", "McpCallResult"),
    "McpClientFactory": ("app.integrations.mcp.ports", "McpClientFactory"),
    "McpManager": ("app.integrations.mcp.manager", "McpManager"),
    "McpManagerError": ("app.integrations.mcp.manager", "McpManagerError"),
    "McpRemoteClient": ("app.integrations.mcp.ports", "McpRemoteClient"),
    "McpRemoteTool": ("app.integrations.mcp.models", "McpRemoteTool"),
    "McpServerState": ("app.integrations.mcp.models", "McpServerState"),
    "McpToolDescriptor": ("app.integrations.mcp.models", "McpToolDescriptor"),
    "SdkMcpClient": ("app.integrations.mcp.client", "SdkMcpClient"),
    "sdk_client_factory": ("app.integrations.mcp.client", "sdk_client_factory"),
}

__all__ = [
    "McpCallPayload",
    "McpCallResult",
    "McpClientFactory",
    "McpManager",
    "McpManagerError",
    "McpRemoteClient",
    "McpRemoteTool",
    "McpServerState",
    "McpToolDescriptor",
    "SdkMcpClient",
    "sdk_client_factory",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
