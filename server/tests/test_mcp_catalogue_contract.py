"""Selected tools and confirmed steps retain their issued catalogue contract."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from test_mcp_integration import FakeClient, FakeFactory, remote_tool
from test_mcp_source_fence import change_source, configured
from test_resource_budget import seed
from test_run_cancel_fence import prepared

from app.cognition.action_plan import ActionInvocation, ActionPlanService
from app.cognition.action_registry import build_builtin_action_registry
from app.cognition.action_runner import ToolActionRunner
from app.db import TaskRunRecord
from app.harness.budget import tool_budget_scope
from app.integrations.mcp import McpManager
from app.integrations.mcp.actions import sync_mcp_actions
from app.integrations.mcp.chat_tools import McpChatToolProvider
from app.integrations.mcp.models import McpCallPayload, McpRemoteTool
from app.runs.resources import resource_usage
from app.tools import ToolContext, ToolExecutor, ToolRegistry
from app.tools.mcp_actions import McpToolCallArgs, McpToolCallTool


def tool(*, write: bool = False, field: str = "q") -> McpRemoteTool:
    return replace(
        remote_tool("create" if write else "search", read_only=not write),
        input_schema={
            "type": "object",
            "properties": {field: {"type": "string"}},
            "required": [field],
        },
    )


async def test_refreshed_schema_rebuilds_model_and_old_handler_is_not_dispatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_FIXTURE_SECRET", "accepted")
    store = await configured(tmp_path)
    factory = FakeFactory(
        [
            FakeClient({None: ([tool()], None)}),
            FakeClient({None: ([tool(field="query")], None)}),
            FakeClient({None: ([], None)}),
        ]
    )
    manager = McpManager(store, client_factory=factory)
    provider = McpChatToolProvider(manager)
    await manager.refresh_server("books")
    (old,) = provider.select("books search", config=store.current.config, privacy_level="L1")
    await manager.refresh_server("books")
    (current,) = provider.select("books search", config=store.current.config, privacy_level="L1")
    assert set(current.arguments_model.model_fields) == {"query"}
    denied = await old.execute(
        old.arguments_model.model_validate({"q": "fixture"}),
        ToolContext(privacy_level="L1", user_id=None),
    )
    assert not denied.ok and denied.reason_code == "mcp_tool_source_changed"
    assert factory.created == 2
    accepted = await current.execute(
        current.arguments_model.model_validate({"query": "fixture"}),
        ToolContext(privacy_level="L1", user_id=None),
    )
    assert accepted.ok and factory.created == 3


@pytest.mark.parametrize("change", ["endpoint", "env_rotate", "rollback", "schema", "restart"])
@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_confirmed_write_plan_cannot_use_reissued_catalogue(
    backend: str, change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_FIXTURE_SECRET", "accepted")
    storage = await prepared(backend, tmp_path)
    try:
        owner, root_id, budget = await seed(storage.database)
        store = await configured(tmp_path)
        client = FakeClient({None: ([tool(write=True)], None)})
        factory = FakeFactory([client])
        manager = McpManager(store, client_factory=factory)
        registry = build_builtin_action_registry()
        manager.set_catalog_listener(lambda: sync_mcp_actions(registry, manager))
        await manager.refresh_server("books")
        runner = ToolActionRunner(ToolExecutor(ToolRegistry([McpToolCallTool(manager)])))
        plans = ActionPlanService(storage.database, registry, runner=runner)
        plan = await plans.create_plan(
            user_id=owner,
            title="Synthetic write",
            invocations=[
                ActionInvocation(
                    action_id="mcp.books.create", arguments={"arguments": {"q": "fixture"}}
                )
            ],
            idempotency_key="synthetic-catalogue-plan",
        )
        await plans.confirm_plan(user_id=owner, plan_id=plan.id)
        if change == "schema":
            client.pages = {None: ([tool(write=True, field="query")], None)}
        elif change == "restart":
            await manager.stop()
            manager = McpManager(store, client_factory=factory)
            runner = ToolActionRunner(ToolExecutor(ToolRegistry([McpToolCallTool(manager)])))
            plans.set_runner(runner)
        else:
            await change_source(store, change, monkeypatch)
        await manager.refresh_server("books")
        with tool_budget_scope(budget):
            result = await plans.execute_plan(user_id=owner, plan_id=plan.id)
        assert str(result.status) == "failed"
        assert result.steps[0].reason_code == "mcp_tool_source_changed"
        assert result.steps[0].result is not None
        assert result.steps[0].result["admission_status"] == "not_admitted"
        assert client.calls == [] and factory.created == 2
        async with storage.database.sessions() as sql:
            root = await sql.get_one(TaskRunRecord, root_id)
            assert resource_usage(root).get("tool_attempts", 0) == 0
        await manager.stop()
    finally:
        await storage.close()


async def test_same_contract_refresh_keeps_confirmed_step_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_FIXTURE_SECRET", "accepted")
    client = FakeClient({None: ([tool(write=True)], None)})
    manager = McpManager(await configured(tmp_path), client_factory=FakeFactory([client]))
    registry = build_builtin_action_registry()
    await manager.refresh_server("books")
    sync_mcp_actions(registry, manager)
    compiled = registry.compile("mcp.books.create", {"arguments": {"q": "fixture"}})
    await manager.refresh_server("books")
    result = await McpToolCallTool(manager).execute(
        McpToolCallArgs.model_validate(compiled.tool_arguments),
        ToolContext(privacy_level="L1", user_id=None),
    )
    assert result.ok and client.calls == [("create", {"q": "fixture"})]


async def test_legacy_write_step_without_catalogue_source_is_not_dispatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_FIXTURE_SECRET", "accepted")
    client = FakeClient({None: ([tool(write=True)], None)})
    factory = FakeFactory([client])
    manager = McpManager(await configured(tmp_path), client_factory=factory)
    await manager.refresh_server("books")
    result = await McpToolCallTool(manager).execute(
        McpToolCallArgs(tool="mcp.books.create", arguments={"q": "fixture"}),
        ToolContext(privacy_level="L1", user_id=None),
    )
    assert not result.ok and result.admission_status == "not_admitted"
    assert result.reason_code == "mcp_tool_source_missing"
    assert client.calls == [] and factory.created == 1


@pytest.mark.parametrize("write", [False, True])
async def test_inflight_call_cannot_return_under_changed_catalogue(
    write: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_FIXTURE_SECRET", "accepted")
    entered, release = asyncio.Event(), asyncio.Event()

    class Blocking(FakeClient):
        async def call_tool(self, name: str, arguments: dict[str, object]) -> McpCallPayload:
            entered.set()
            await release.wait()
            return self.payload

    factory = FakeFactory(
        [
            FakeClient({None: ([tool(write=write)], None)}),
            Blocking({None: ([], None)}),
            FakeClient({None: ([tool(write=write, field="query")], None)}),
        ]
    )
    manager = McpManager(await configured(tmp_path), client_factory=factory)
    await manager.refresh_server("books")
    pending = asyncio.create_task(
        manager.call_write("mcp.books.create", {})
        if write
        else manager.call("mcp.books.search", {})
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await manager.refresh_server("books")
        release.set()
        result = await pending
        assert not result.ok and result.reason_code == "mcp_tool_source_changed"
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await manager.stop()
