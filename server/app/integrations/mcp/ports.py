"""MCP request ports independent of transport SDKs and persistence."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from types import TracebackType
from typing import Any, Literal, Protocol, Self, TypeVar
from uuid import UUID

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


T = TypeVar("T")


class McpOperationRunner(Protocol):
    async def __call__(
        self,
        config: McpServerConfig,
        operation: Literal["refresh", "read", "write"],
        invoke: Callable[[], Awaitable[T]],
        source_guard: Callable[[], Awaitable[None]],
        closing: Callable[[], bool],
        *,
        user_id: UUID | None = None,
    ) -> T: ...


class McpRequestRunner(Protocol):
    async def __call__(self, method: str, invoke: Callable[[], Awaitable[T]]) -> T: ...


_REQUEST_RUNNER: ContextVar[McpRequestRunner | None] = ContextVar("mcp_http_request", default=None)


def current_request_runner() -> McpRequestRunner | None:
    return _REQUEST_RUNNER.get()


@contextmanager
def request_runner_scope(runner: McpRequestRunner) -> Iterator[None]:
    token = _REQUEST_RUNNER.set(runner)
    try:
        yield
    finally:
        _REQUEST_RUNNER.reset(token)
