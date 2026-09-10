from __future__ import annotations

from contextlib import AsyncExitStack
from types import TracebackType
from typing import Any, Protocol, Self

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from app.config.models import McpServerConfig
from app.llm.provider import EnvSecretProvider

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


class SdkMcpClient:
    """Small boundary around the official MCP SDK v2."""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        secrets: EnvSecretProvider | None = None,
    ) -> None:
        self._config = config
        self._secrets = secrets or EnvSecretProvider()
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self.protocol_version: str | None = None
        self.server_name: str | None = None
        self.server_version: str | None = None

    async def __aenter__(self) -> Self:
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            headers: dict[str, str] = {}
            secret = self._config.secret_value
            if secret is None and self._config.secret_ref is not None:
                secret = self._secrets.resolve(self._config.secret_ref)
            if secret is not None:
                headers["Authorization"] = f"Bearer {secret}"
            timeout = httpx2.Timeout(
                self._config.call_timeout_seconds,
                connect=self._config.connect_timeout_seconds,
            )
            http_client = await stack.enter_async_context(
                httpx2.AsyncClient(headers=headers, timeout=timeout)
            )
            transport = streamable_http_client(
                str(self._config.endpoint), http_client=http_client
            )
            client = await stack.enter_async_context(
                Client(
                    transport,
                    read_timeout_seconds=self._config.call_timeout_seconds,
                )
            )
            self._stack = stack
            self._client = client
            self.protocol_version = client.protocol_version
            info = client.server_info
            self.server_name = info.name if info is not None else None
            self.server_version = info.version if info is not None else None
            return self
        except BaseException:
            await stack.aclose()
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        stack, self._stack = self._stack, None
        self._client = None
        if stack is not None:
            await stack.aclose()

    def _require_client(self) -> Client:
        if self._client is None:
            raise RuntimeError("MCP client is not connected")
        return self._client

    async def list_tools(
        self, cursor: str | None = None
    ) -> tuple[list[McpRemoteTool], str | None]:
        result = await self._require_client().list_tools(cursor=cursor)
        tools = []
        for tool in result.tools:
            annotations = tool.annotations
            tools.append(
                McpRemoteTool(
                    name=tool.name,
                    title=tool.title,
                    description=(tool.description or "")[:1_000],
                    input_schema=dict(tool.input_schema),
                    read_only_hint=(
                        annotations.read_only_hint if annotations is not None else None
                    ),
                    destructive_hint=(
                        annotations.destructive_hint if annotations is not None else None
                    ),
                    idempotent_hint=(
                        annotations.idempotent_hint if annotations is not None else None
                    ),
                )
            )
        return tools, result.next_cursor

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpCallPayload:
        result = await self._require_client().call_tool(
            name,
            arguments,
            read_timeout_seconds=self._config.call_timeout_seconds,
        )
        text = "\n".join(
            block.text for block in result.content if isinstance(block, TextContent)
        )
        return McpCallPayload(
            is_error=result.is_error,
            structured_content=result.structured_content,
            text=text,
        )


def sdk_client_factory(config: McpServerConfig) -> McpRemoteClient:
    return SdkMcpClient(config)
