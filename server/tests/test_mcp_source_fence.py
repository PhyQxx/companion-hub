"""MCP catalogues and results belong to their accepted connection epoch."""

import asyncio
from pathlib import Path
from types import TracebackType
from typing import Self

import httpx2
import pytest
import yaml
from test_mcp_integration import FakeClient, FakeFactory, make_config, remote_tool

from app.config import ConfigStore
from app.integrations.mcp import McpManager, McpManagerError
from app.integrations.mcp.models import McpCallPayload, McpCallResult, McpRemoteTool, McpServerState


async def configured(tmp_path: Path) -> ConfigStore:
    value = make_config(allow_write=True).model_dump(mode="json")
    value["mcp"]["servers"][0]["secret_ref"] = "env:MCP_FIXTURE_SECRET"
    path = tmp_path / "mcp.yaml"
    path.write_text(yaml.safe_dump(value))
    store = ConfigStore(path)
    await store.load()
    return store


async def change_source(store: ConfigStore, change: str, monkeypatch: pytest.MonkeyPatch) -> None:
    value = store.current.config.model_dump(mode="json")
    server = value["mcp"]["servers"][0]
    if change == "endpoint":
        server["endpoint"] = "https://other.example.test/mcp"
    elif change == "allowlist":
        server["allowed_tools"] = ["create"]
    elif change == "writes":
        server["allow_write_tools"] = False
    elif change == "removed":
        value["mcp"]["servers"] = []
    elif change == "env_rotate":
        monkeypatch.setenv("MCP_FIXTURE_SECRET", "changed")
        return
    elif change == "env_remove":
        monkeypatch.delenv("MCP_FIXTURE_SECRET")
        return
    else:
        value["mcp"]["enabled"] = False
    original = store.path.read_text()
    store.path.write_text(yaml.safe_dump(value))
    await store.reload()
    if change == "rollback":
        store.path.write_text(original)
        await store.reload()


@pytest.mark.parametrize(
    "change",
    [
        "endpoint",
        "allowlist",
        "writes",
        "removed",
        "disabled",
        "rollback",
        "env_rotate",
        "env_remove",
    ],
)
async def test_cached_catalogue_is_invalidated_before_new_connection(
    change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_FIXTURE_SECRET", "accepted")
    store = await configured(tmp_path)
    client = FakeClient(
        {None: ([remote_tool("search"), remote_tool("create", read_only=False)], None)}
    )
    factory = FakeFactory([client])
    manager = McpManager(store, client_factory=factory)
    await manager.refresh_server("books")
    await change_source(store, change, monkeypatch)
    assert manager.catalog() == ()
    for name, invoke in [
        ("mcp.books.search", manager.call),
        ("mcp.books.create", manager.call_write),
    ]:
        with pytest.raises(McpManagerError, match="mcp_tool_unavailable"):
            await invoke(name, {})
    assert factory.created == 1 and client.calls == []


@pytest.mark.parametrize(
    "change", ["endpoint", "rollback", "env_rotate", "env_remove", "disabled", "stop"]
)
@pytest.mark.parametrize("operation", ["refresh", "read", "write"])
async def test_inflight_results_do_not_publish_after_source_withdrawal(
    change: str, operation: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_FIXTURE_SECRET", "accepted")
    store = await configured(tmp_path)
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    tools = [remote_tool("search"), remote_tool("create", read_only=False)]

    class Blocking(FakeClient):
        async def list_tools(
            self, cursor: str | None = None
        ) -> tuple[list[McpRemoteTool], str | None]:
            entered.set()
            await release.wait()
            return tools, None

        async def call_tool(self, name: str, arguments: dict[str, object]) -> McpCallPayload:
            entered.set()
            await release.wait()
            return self.payload

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            await asyncio.sleep(0.35)
            closed.set()

    blocking = Blocking({None: (tools, None)})
    factory = FakeFactory(
        [FakeClient({None: (tools, None)}), blocking] if operation != "refresh" else [blocking]
    )
    manager = McpManager(store, client_factory=factory)
    publications: list[tuple[str, ...]] = []
    manager.set_catalog_listener(
        lambda: publications.append(tuple(item.internal_name for item in manager.catalog()))
    )
    if operation != "refresh":
        await manager.refresh_server("books")
        publications.clear()
    pending = asyncio.create_task(
        manager.refresh_server("books")
        if operation == "refresh"
        else manager.call("mcp.books.search", {})
        if operation == "read"
        else manager.call_write("mcp.books.create", {})
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if change == "stop":
            stopping = asyncio.create_task(manager.stop())
            await asyncio.sleep(0)
        else:
            await change_source(store, change, monkeypatch)
        release.set()
        try:
            result = await pending
        except McpManagerError:
            result = None
        except asyncio.CancelledError:
            assert change == "stop"
            result = None
        if change == "stop":
            await stopping
        if result is not None:
            if operation == "refresh":
                assert isinstance(result, McpServerState) and not result.available
            else:
                assert isinstance(result, McpCallResult) and not result.ok
        assert closed.is_set()
        assert not any(publications) and manager.catalog() == ()
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await manager.stop()


async def test_catalogue_snapshots_cannot_mutate_accepted_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_FIXTURE_SECRET", "accepted")
    tool = remote_tool("search")
    manager = McpManager(
        await configured(tmp_path), client_factory=FakeFactory([FakeClient({None: ([tool], None)})])
    )
    state = await manager.refresh_server("books")
    state.tools["mcp.books.search"].input_schema["type"] = "array"
    manager.catalog()[0].input_schema["type"] = "string"
    manager.states[0].tools.clear()
    tool.input_schema["type"] = "number"
    assert manager.catalog()[0].input_schema["type"] == "object"


@pytest.mark.parametrize("change", ["endpoint", "rollback", "stop"])
async def test_queued_refresh_keeps_original_source_epoch(
    change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_FIXTURE_SECRET", "accepted")
    store = await configured(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    class Blocking(FakeClient):
        async def list_tools(
            self, cursor: str | None = None
        ) -> tuple[list[McpRemoteTool], str | None]:
            entered.set()
            await release.wait()
            return [], None

    factory = FakeFactory([Blocking({None: ([], None)})])
    manager = McpManager(store, client_factory=factory)
    first = asyncio.create_task(manager.refresh_server("books"))
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(manager.refresh_server("books"))
    try:
        await asyncio.sleep(0)
        if change == "stop":
            await manager.stop()
        else:
            await change_source(store, change, monkeypatch)
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(
            isinstance(value, (McpManagerError, asyncio.CancelledError)) for value in results
        )
        assert factory.created == 1
    finally:
        release.set()
        for pending in (first, second):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        await manager.stop()


async def test_native_failed_connect_joins_cleanup_on_owning_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.integrations.mcp import client as sdk

    monkeypatch.setenv("MCP_FIXTURE_SECRET", "accepted")
    store = await configured(tmp_path)
    closing, closed = asyncio.Event(), asyncio.Event()

    class Stack:
        def __init__(self) -> None:
            self.owner = asyncio.current_task()
            self.entered = 0

        async def __aenter__(self) -> Self:
            return self

        async def enter_async_context(self, value: object) -> object:
            self.entered += 1
            if self.entered == 2:
                raise ConnectionError("synthetic connection failure")
            return value

        async def aclose(self) -> None:
            assert asyncio.current_task() is self.owner
            closing.set()
            await asyncio.sleep(0.35)
            closed.set()

    monkeypatch.setattr(sdk, "AsyncExitStack", Stack)
    monkeypatch.setattr(httpx2, "AsyncClient", lambda **kwargs: object())
    monkeypatch.setattr(sdk, "streamable_http_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(sdk, "Client", lambda *args, **kwargs: object())
    manager = McpManager(store)
    pending = asyncio.create_task(manager.refresh_server("books"))
    try:
        await asyncio.wait_for(closing.wait(), 2)
        await change_source(store, "env_rotate", monkeypatch)
        with pytest.raises(McpManagerError, match="mcp_source_changed"):
            await pending
        assert closed.is_set() and manager.catalog() == ()
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await manager.stop()


async def test_cancelled_stop_waits_for_background_disconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_FIXTURE_SECRET", "accepted")
    entered, closing, release, closed = (asyncio.Event() for _ in range(4))

    class Blocking(FakeClient):
        async def list_tools(
            self, cursor: str | None = None
        ) -> tuple[list[McpRemoteTool], str | None]:
            entered.set()
            await asyncio.Event().wait()
            return [], None

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            closing.set()
            await release.wait()
            closed.set()

    manager = McpManager(
        await configured(tmp_path), client_factory=FakeFactory([Blocking({None: ([], None)})])
    )
    manager.start()
    await asyncio.wait_for(entered.wait(), 2)
    pending = asyncio.create_task(manager.stop())
    try:
        await asyncio.wait_for(closing.wait(), 2)
        pending.cancel()
        pending.cancel()
        await asyncio.sleep(0.35)
        assert not pending.done() and not closed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert closed.is_set() and manager.catalog() == ()
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await manager.stop()
