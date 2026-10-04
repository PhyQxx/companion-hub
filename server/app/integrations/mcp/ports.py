"""MCP request ports independent of transport SDKs and persistence."""

from __future__ import annotations

from types import TracebackType
from typing import Any, Protocol, Self

from app.config.models import McpServerConfig

from .models import McpCallPayload, McpRemoteTool


class McpRemoteClient(Protocol):
    protocol_version: str | None
    server_name: str | None
    server_version: str | None

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def list_tools(
        self, cursor: str | None = None
    ) -> tuple[list[McpRemoteTool], str | None]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpCallPayload: ...


class McpClientFactory(Protocol):
    def __call__(self, config: McpServerConfig) -> McpRemoteClient: ...
