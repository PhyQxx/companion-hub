from __future__ import annotations

from types import SimpleNamespace, TracebackType
from typing import Any, Self, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api import create_admin_mcp_router
from app.config import HubConfig, McpServerConfig
from app.integrations.mcp import (
    McpCallPayload,
    McpManager,
    McpManagerError,
    McpRemoteClient,
    McpRemoteTool,
)


def make_config(*, enabled: bool = True, allow_write: bool = False) -> HubConfig:
    return HubConfig.model_validate(
        {
            "schema_version": 1,
            "models": {
                "cloud": {
                    "provider": "openai_compatible",
                    "model": "test-model",
                    "base_url": "https://models.example/v1",
                    "runs_local": False,
                    "max_privacy_level": "L1",
                    "max_context_tokens": 32768,
                    "input_cost_per_million": 0,
                    "output_cost_per_million": 0,
                },
                "local": {
                    "provider": "openai_compatible",
                    "model": "local-model",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "runs_local": True,
                    "max_privacy_level": "L2",
                    "max_context_tokens": 32768,
                    "input_cost_per_million": 0,
                    "output_cost_per_million": 0,
                },
            },
            "routes": {
                "dialogue": {"primary": "cloud"},
                "utility": {"primary": "cloud"},
                "private": {"primary": "local"},
            },
            "mcp": {
                "enabled": enabled,
                "servers": [
                    {
                        "server_id": "books",
                        "enabled": enabled,
                        "endpoint": "https://mcp.example.test/mcp",
                        "allowed_tools": ["search", "create"],
                        "allow_write_tools": allow_write,
                        "catalog_ttl_seconds": 300,
                        "max_result_bytes": 1024,
                    }
                ],
            },
        }
    )


def make_store(config: HubConfig) -> Any:
    return SimpleNamespace(current=SimpleNamespace(config=config))


def remote_tool(
    name: str,
    *,
    read_only: bool | None = True,
    destructive: bool | None = False,
) -> McpRemoteTool:
    return McpRemoteTool(
        name=name,
        title=name.title(),
        description=f"{name} remote description",
        input_schema={"type": "object", "properties": {}},
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=True,
    )


class FakeClient:
    protocol_version: str | None = "2026-07-28"
    server_name: str | None = "Book Server"
    server_version: str | None = "1.2.3"

    def __init__(
        self,
        pages: dict[str | None, tuple[list[McpRemoteTool], str | None]],
        *,
        payload: McpCallPayload | None = None,
        error: Exception | None = None,
    ) -> None:
        self.pages = pages
        self.payload = payload or McpCallPayload(False, {"ok": True}, "done")
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> Self:
        if self.error is not None:
            raise self.error
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    async def list_tools(
        self, cursor: str | None = None
    ) -> tuple[list[McpRemoteTool], str | None]:
        return self.pages[cursor]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpCallPayload:
        self.calls.append((name, arguments))
        return self.payload


class FakeFactory:
    def __init__(self, clients: list[FakeClient]) -> None:
        self.clients = clients
        self.created = 0

    def __call__(self, config: McpServerConfig) -> McpRemoteClient:
        assert config.server_id == "books"
        client = self.clients[min(self.created, len(self.clients) - 1)]
        self.created += 1
        return client


def test_mcp_config_rejects_unsafe_endpoints_and_accepts_explicit_loopback() -> None:
    with pytest.raises(ValidationError, match="must use HTTPS"):
        McpServerConfig(
            server_id="unsafe",
            endpoint="http://example.com/mcp",
        )
    with pytest.raises(ValidationError, match="cannot contain credentials"):
        McpServerConfig(
            server_id="unsafe",
            endpoint="https://user:secret@example.com/mcp",
        )
    local = McpServerConfig(
        server_id="local",
        endpoint="http://127.0.0.1:8765/mcp",
        allow_insecure_local_http=True,
    )
    assert str(local.endpoint).startswith("http://127.0.0.1:8765/")


async def test_disabled_mcp_makes_no_connections() -> None:
    factory = FakeFactory([FakeClient({None: ([], None)})])
    manager = McpManager(
        cast(Any, make_store(make_config(enabled=False))),
        client_factory=factory,
    )

    await manager.refresh_all()

    assert factory.created == 0
    assert manager.catalog() == ()
    assert manager.states[0].configured_enabled is False


async def test_catalog_is_paginated_allowlisted_namespaced_and_read_only_by_default() -> None:
    client = FakeClient(
        {
            None: ([remote_tool("search"), remote_tool("hidden")], "next"),
            "next": ([remote_tool("create", read_only=None, destructive=True)], None),
        }
    )
    manager = McpManager(
        cast(Any, make_store(make_config())),
        client_factory=FakeFactory([client]),
    )

    state = await manager.refresh_server("books")

    assert state.available is True
    assert state.protocol_version == "2026-07-28"
    assert state.server_name == "Book Server"
    assert list(state.tools) == ["mcp.books.search"]
    descriptor = manager.catalog()[0]
    assert descriptor.remote_name == "search"
    assert descriptor.read_only is True


async def test_remote_failure_keeps_stable_status_reason() -> None:
    manager = McpManager(
        cast(Any, make_store(make_config())),
        client_factory=FakeFactory(
            [FakeClient({None: ([], None)}, error=ConnectionError("secret host detail"))]
        ),
    )

    state = await manager.refresh_server("books")

    assert state.available is False
    assert state.last_error == "mcp_connection_failed"
    assert state.consecutive_failures == 1


async def test_read_only_call_is_bounded_and_remote_error_is_structured() -> None:
    discover = FakeClient({None: ([remote_tool("search")], None)})
    call = FakeClient(
        {None: ([], None)},
        payload=McpCallPayload(True, {"value": "x" * 2_000}, "private remote detail"),
    )
    manager = McpManager(
        cast(Any, make_store(make_config())),
        client_factory=FakeFactory([discover, call]),
    )
    await manager.refresh_server("books")

    result = await manager.call("mcp.books.search", {"query": "dune"})

    assert result.ok is False
    assert result.reason_code == "mcp_remote_tool_error"
    assert result.data["truncated"] is True
    assert len(result.text.encode()) <= 1024
    assert call.calls == [("search", {"query": "dune"})]


async def test_write_tool_cannot_use_direct_call_path() -> None:
    client = FakeClient({None: ([remote_tool("create", read_only=False)], None)})
    manager = McpManager(
        cast(Any, make_store(make_config(allow_write=True))),
        client_factory=FakeFactory([client]),
    )
    await manager.refresh_server("books")

    with pytest.raises(McpManagerError, match="mcp_write_requires_action_plan"):
        await manager.call("mcp.books.create", {})


async def test_admin_mcp_status_catalog_refresh_and_auth() -> None:
    manager = McpManager(
        cast(Any, make_store(make_config())),
        client_factory=FakeFactory([FakeClient({None: ([remote_tool("search")], None)})]),
    )
    app = FastAPI()
    app.include_router(create_admin_mcp_router(manager, admin_token="admin-secret"))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.get("/api/v1/admin/mcp/servers")
        refreshed = await client.post(
            "/api/v1/admin/mcp/servers/books/refresh",
            headers={"Authorization": "Bearer admin-secret"},
        )
        tools = await client.get(
            "/api/v1/admin/mcp/tools",
            headers={"Authorization": "Bearer admin-secret"},
        )

    assert denied.status_code == 401
    assert refreshed.status_code == 200
    assert refreshed.json()["tool_count"] == 1
    assert tools.json()["items"][0]["internal_name"] == "mcp.books.search"
