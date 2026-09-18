from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any, Self, cast
from uuid import uuid4

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
    McpServerState,
    McpToolDescriptor,
)


def make_config(
    *, enabled: bool = True, allow_write: bool = False, allowed_tools: list[str] | None = None
) -> HubConfig:
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
                        "allowed_tools": allowed_tools or ["search", "create"],
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


def _write_catalog_config(allow_write: bool = True) -> HubConfig:
    return make_config(allow_write=allow_write, allowed_tools=["search", "create", "fancy"])


def _write_tool_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string", "maxLength": 120},
            "copies": {"type": "integer", "minimum": 1, "maximum": 10},
            "draft": {"type": "boolean"},
        },
        "required": ["title"],
    }


def test_build_arguments_model_accepts_only_conservative_schemas() -> None:
    from app.integrations.mcp.actions import build_arguments_model

    model = build_arguments_model(_write_tool_schema())
    assert model is not None
    validated = cast(
        Any, model.model_validate({"arguments": {"title": "书名", "copies": 2}})
    )
    assert validated.arguments.title == "书名"
    with pytest.raises(ValidationError):
        model.model_validate({"arguments": {"title": "x", "copies": 99}})
    with pytest.raises(ValidationError):
        model.model_validate({"arguments": {"title": "x", "extra": 1}})

    assert build_arguments_model({"type": "object", "properties": {"a": {"type": "array"}}}) is None
    assert build_arguments_model({"type": "object", "anyOf": []}) is None
    assert build_arguments_model({"type": "object", "properties": {}}) is None
    assert build_arguments_model({"type": "string"}) is None


async def test_sync_mcp_actions_registers_only_write_tools_with_a2_policy() -> None:
    from app.cognition.action_registry import (
        ActionRisk,
        ConfirmationPolicy,
        build_builtin_action_registry,
    )
    from app.integrations.mcp.actions import MCP_TOOL_NAME, sync_mcp_actions

    client = FakeClient(
        {
            None: (
                [
                    remote_tool("search", read_only=True),
                    McpRemoteTool(
                        name="create",
                        title="Create Book",
                        description="create remotely",
                        input_schema=_write_tool_schema(),
                        read_only_hint=False,
                        destructive_hint=False,
                        idempotent_hint=False,
                    ),
                    McpRemoteTool(
                        name="fancy",
                        title="Fancy",
                        description="unsupported schema",
                        input_schema={"type": "object", "anyOf": [{}]},
                        read_only_hint=False,
                        destructive_hint=False,
                        idempotent_hint=True,
                    ),
                ],
                None,
            )
        }
    )
    manager = McpManager(make_store(_write_catalog_config()), client_factory=FakeFactory([client]))
    await manager.refresh_all()
    registry = build_builtin_action_registry()

    report = sync_mcp_actions(registry, manager)

    assert report.registered == ("mcp.books.create",)
    assert report.removed == ()
    assert [name for name, _ in report.skipped] == ["mcp.books.fancy"]
    action = registry.require("mcp.books.create")
    assert action.definition.risk == ActionRisk.A2_CONFIRM.value
    assert action.definition.confirmation_policy == ConfirmationPolicy.ALWAYS.value
    assert action.definition.tool_name == MCP_TOOL_NAME
    assert action.definition.bound_arguments == {"tool": "mcp.books.create"}
    compiled = registry.compile(
        "mcp.books.create", {"arguments": {"title": "新书", "copies": 3}}
    )
    assert compiled.tool_arguments == {
        "tool": "mcp.books.create",
        "arguments": {"title": "新书", "copies": 3},
    }

    # 重复同步幂等；目录清空后动作被移除
    sync_mcp_actions(registry, manager)
    assert registry.get("mcp.books.create") is not None
    client.pages = {None: ([remote_tool("search")], None)}
    manager2 = McpManager(make_store(_write_catalog_config()), client_factory=FakeFactory([client]))
    await manager2.refresh_all()
    report2 = sync_mcp_actions(registry, manager2)
    assert report2.removed == ("mcp.books.create",)
    assert registry.get("mcp.books.create") is None
    for item in (manager, manager2):
        await item.stop()


async def test_catalog_listener_fires_after_successful_refresh() -> None:
    fired: list[int] = []
    client = FakeClient({None: ([remote_tool("search")], None)})
    manager = McpManager(make_store(make_config()), client_factory=FakeFactory([client]))
    manager.set_catalog_listener(lambda: fired.append(1))
    await manager.refresh_all()
    assert fired == [1]
    await manager.stop()


async def test_mcp_tool_call_tool_routes_through_call_write_and_bounds_payload() -> None:
    from app.tools import ToolContext
    from app.tools.mcp_actions import McpToolCallArgs, McpToolCallTool

    client = FakeClient(
        {None: ([], None)},
        payload=McpCallPayload(False, {"created": True}, "created"),
    )
    manager = McpManager(make_store(make_config()), client_factory=FakeFactory([client]))
    # 目录中注入一个写工具（FakeFactory 固定 server books）
    manager._states["books"] = cast(McpServerState, SimpleNamespace(
        configured_enabled=True,
        available=True,
        tools={
            "mcp.books.create": SimpleNamespace(
                internal_name="mcp.books.create",
                server_id="books",
                remote_name="create",
                title="Create",
                description="",
                input_schema={},
                read_only=False,
                destructive=False,
                idempotent=False,
            )
        },
        tool_count=1,
        protocol_version="2026-07-28",
        server_name="Books",
        server_version="1",
        last_refresh_at=None,
        last_error=None,
        refreshing=False,
        consecutive_failures=0,
    ))
    tool = McpToolCallTool(manager)

    ok = await tool.execute(
        McpToolCallArgs(tool="mcp.books.create", arguments={"title": "新书"}),
        ToolContext(privacy_level="L1", user_id=None),
    )
    assert ok.ok is True
    assert client.calls == [("create", {"title": "新书"})]
    assert ok.data["server_id"] == "books"
    assert ok.data["ok"] is True

    oversized = await tool.execute(
        McpToolCallArgs(tool="mcp.books.create", arguments={"title": "长" * 9_000}),
        ToolContext(privacy_level="L1", user_id=None),
    )
    assert oversized.ok is False
    assert oversized.reason_code == "mcp_arguments_too_large"

    missing = await tool.execute(
        McpToolCallArgs(tool="mcp.books.missing", arguments={}),
        ToolContext(privacy_level="L1", user_id=None),
    )
    assert missing.ok is False
    assert missing.reason_code == "mcp_tool_unavailable"
    await manager.stop()


async def test_manager_call_still_blocks_write_but_call_write_executes() -> None:
    client = FakeClient(
        {None: ([], None)},
        payload=McpCallPayload(False, {"created": True}, "created"),
    )
    manager = McpManager(make_store(make_config()), client_factory=FakeFactory([client]))
    manager._states["books"] = cast(McpServerState, SimpleNamespace(
        configured_enabled=True,
        available=True,
        tools={
            "mcp.books.create": SimpleNamespace(
                internal_name="mcp.books.create",
                server_id="books",
                remote_name="create",
                title="Create",
                description="",
                input_schema={},
                read_only=False,
                destructive=False,
                idempotent=False,
            )
        },
        tool_count=1,
        protocol_version="2026-07-28",
        server_name="Books",
        server_version="1",
        last_refresh_at=None,
        last_error=None,
        refreshing=False,
        consecutive_failures=0,
    ))
    with pytest.raises(McpManagerError, match="mcp_write_requires_action_plan"):
        await manager.call("mcp.books.create", {})
    result = await manager.call_write("mcp.books.create", {"title": "x"})
    assert result.ok is True
    await manager.stop()


async def test_plan_runner_executes_mcp_step_with_l1_context_and_receipt() -> None:
    from uuid import uuid4

    from app.cognition.action_plan import ActionStepView, ActionVerificationStatus
    from app.cognition.action_runner import ToolActionRunner
    from app.tools import ToolExecutor, ToolRegistry, ToolResult
    from app.tools.mcp_actions import McpToolCallTool

    class RecordingTool(McpToolCallTool):
        def __init__(self) -> None:
            super().__init__(cast(Any, SimpleNamespace()))
            self.contexts: list[Any] = []

        async def execute(self, args: Any, context: Any) -> ToolResult:
            self.contexts.append(context)
            return ToolResult(
                ok=True,
                tool_name=self.name,
                provider="mcp",
                latency_ms=1.0,
                data={"server_id": "books", "tool_name": args.tool, "ok": True},
            )

    tool = RecordingTool()
    runner = ToolActionRunner(ToolExecutor(ToolRegistry([tool])))
    step = ActionStepView(
        id=uuid4(),
        position=0,
        action_id="mcp.books.create",
        risk="A2",
        confirmation_policy="always",
        status="executing",
        arguments={"arguments": {"title": "新书"}},
        tool_name="mcp_tool_call",
        tool_arguments={"tool": "mcp.books.create", "arguments": {"title": "新书"}},
        idempotency_key=f"step-{uuid4()}",
        timeout_seconds=15,
        verification_policy="receipt",
        verifier_id="mcp.call_receipt",
        verification_status="pending",
    )

    result = await runner(step, uuid4())

    assert result.execution.ok is True
    assert tool.contexts[0].privacy_level == "L1"
    assert result.verification_status == ActionVerificationStatus.VERIFIED.value
    assert result.verification_result == {
        "server_id": "books",
        "tool_name": "mcp.books.create",
        "ok": True,
    }


def _context7_catalog_factory() -> FakeFactory:
    """目录含两个只读工具（query-docs 带 schema）+ 一个写工具。"""
    return FakeFactory(
        [
            FakeClient(
                {
                    None: (
                        [
                            McpRemoteTool(
                                name="resolve-library-id",
                                title="Resolve Context7 Library ID",
                                description="remote free text must not reach the model",
                                input_schema={
                                    "type": "object",
                                    "properties": {
                                        "libraryName": {"type": "string"},
                                        "query": {"type": "string"},
                                    },
                                    "required": ["libraryName", "query"],
                                },
                                read_only_hint=True,
                                destructive_hint=False,
                                idempotent_hint=True,
                            ),
                            McpRemoteTool(
                                name="query-docs",
                                title="Query Documentation",
                                description="remote free text 2",
                                input_schema={
                                    "type": "object",
                                    "properties": {
                                        "libraryId": {"type": "string"},
                                        "query": {"type": "string"},
                                    },
                                    "required": ["libraryId", "query"],
                                },
                                read_only_hint=True,
                                destructive_hint=False,
                                idempotent_hint=True,
                            ),
                            McpRemoteTool(
                                name="create-book",
                                title="Create Book",
                                description="write tool",
                                input_schema={
                                    "type": "object",
                                    "properties": {"title": {"type": "string"}},
                                    "required": ["title"],
                                },
                                read_only_hint=False,
                                destructive_hint=False,
                                idempotent_hint=False,
                            ),
                        ],
                        None,
                    )
                },
                payload=McpCallPayload(False, {"answer": 42}, "文档片段"),
            )
        ]
    )


async def _context7_manager(allow_write: bool = True) -> McpManager:
    config = make_config(
        allow_write=allow_write,
        allowed_tools=["resolve-library-id", "query-docs", "create-book"],
    )
    manager = McpManager(make_store(config), client_factory=_context7_catalog_factory())
    await manager.refresh_all()
    return manager


async def test_chat_tool_provider_selects_relevant_read_tools_only() -> None:
    from app.integrations.mcp.chat_tools import McpChatToolProvider

    manager = await _context7_manager()
    provider = McpChatToolProvider(manager)
    config = make_config(
        allow_write=True, allowed_tools=["resolve-library-id", "query-docs", "create-book"]
    )

    selected = provider.select(
        "用 books query docs 查 vue 文档", config=config, privacy_level="L1"
    )
    names = [handler.name for handler in selected]
    assert names == ["mcp.books.query-docs", "mcp.books.resolve-library-id"]
    definition = selected[0].definition()
    assert definition.description.startswith("外部只读工具")
    assert "remote free text" not in definition.description
    assert set(definition.parameters["properties"]) == {"libraryId", "query"}

    # 无关文本零挂载：工具目录增长不推高普通对话 Token
    assert provider.select("今天晚饭吃什么", config=config, privacy_level="L1") == ()
    # L2 回合禁止外发
    assert provider.select("books query docs 查文档", config=config, privacy_level="L2") == ()
    # 总开关关闭
    disabled = make_config(enabled=False)
    assert provider.select("books query docs 查文档", config=disabled, privacy_level="L1") == ()
    await manager.stop()


async def test_chat_tool_provider_bounds_per_turn_and_skips_bad_schema() -> None:
    from app.integrations.mcp.chat_tools import McpChatToolProvider

    config = make_config(
        allow_write=False, allowed_tools=["resolve-library-id", "query-docs", "create-book"]
    )
    config = config.model_copy(
        update={"mcp": config.mcp.model_copy(update={"max_tools_per_turn": 1})}
    )
    manager = await _context7_manager(allow_write=False)
    provider = McpChatToolProvider(manager)

    selected = provider.select("books query docs 文档", config=config, privacy_level="L1")
    assert len(selected) == 1

    # 复杂 schema 的只读工具不被挂载
    broken = McpRemoteTool(
        name="broken",
        title="Broken",
        description="",
        input_schema={"type": "object", "anyOf": []},
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
    )
    manager._states["books"].tools["mcp.books.broken"] = cast(
        McpToolDescriptor,
        SimpleNamespace(
            internal_name="mcp.books.broken",
            server_id="books",
            remote_name="broken",
            title="Broken",
            description="",
            input_schema={"type": "object", "anyOf": []},
            read_only=True,
            destructive=False,
            idempotent=True,
        ),
    )
    del broken
    assert provider.select("books broken 查询", config=config, privacy_level="L1") == ()
    await manager.stop()


async def test_read_tool_handler_executes_via_manager_call() -> None:
    from app.integrations.mcp.chat_tools import McpChatToolProvider
    from app.tools.contracts import ToolContext

    manager = await _context7_manager(allow_write=False)
    provider = McpChatToolProvider(manager)
    config = make_config(allow_write=False)
    handler = provider.select(
        "books query docs 文档", config=config, privacy_level="L1"
    )[0]

    arguments = handler.arguments_model.model_validate(
        {"libraryId": "/websites/vuejs", "query": "reactivity"}
    )
    result = await handler.execute(arguments, ToolContext(privacy_level="L1"))
    assert result.ok is True
    assert result.data["server_id"] == "books"
    assert result.data["text"] == "文档片段"
    await manager.stop()


async def test_chat_service_mounts_mcp_tools_only_for_relevant_turns(
    tmp_path: Path,
) -> None:
    from test_chat import FakeRouter, config_yaml

    from app.chat import ChatService
    from app.config import DatabaseConfigStore
    from app.db import AppUserRecord, Base, create_database
    from app.integrations.mcp.chat_tools import McpChatToolProvider
    from app.memory import MemoryStore
    from app.schemas import PrivacyLevel

    requests: list[Any] = []
    database = create_database("sqlite+aiosqlite:///:memory:")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    owner_id = uuid4()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=owner_id, display_name="MCP", status="active"))
    config_path = tmp_path / "hub.yaml"
    config_path.write_text(
        config_yaml()
        + """
mcp:
  enabled: true
  servers:
    - server_id: books
      enabled: true
      endpoint: https://mcp.example.test/mcp
      allowed_tools: [query-docs]
""",
        encoding="utf-8",
    )
    config_store = DatabaseConfigStore(database, config_path)
    await config_store.load()
    manager = McpManager(
        config_store, client_factory=FakeFactory(
            [
                FakeClient(
                    {None: ([McpRemoteTool(
                        name="query-docs",
                        title="Query Documentation",
                        description="d",
                        input_schema={
                            "type": "object",
                            "properties": {"q": {"type": "string"}},
                            "required": ["q"],
                        },
                        read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                    )], None)},
                    payload=McpCallPayload(False, {"a": 1}, "ok"),
                )
            ]
        )
    )
    await manager.refresh_all()
    service = ChatService(
        database,
        config_store,
        router_builder=lambda config: FakeRouter(config.models["cloud"].model, requests),
        memory_store=MemoryStore(database),
        mcp_tools=McpChatToolProvider(manager),
    )
    conversation = await service.create_conversation(user_id=owner_id, title="mcp")

    await service.send_message(
        conversation.id, user_id=owner_id, text="books 文档怎么写", privacy_level=PrivacyLevel.L1
    )
    mounted = [tool.name for tool in requests[-1].tools]
    assert "mcp.books.query-docs" in mounted

    await service.send_message(
        conversation.id, user_id=owner_id, text="讲个睡前故事", privacy_level=PrivacyLevel.L1
    )
    mounted = [tool.name for tool in requests[-1].tools]
    assert all(not name.startswith("mcp.") for name in mounted)
    await manager.stop()
