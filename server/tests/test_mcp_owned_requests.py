"""Production MCP wiring with the real SDK and isolated HTTP/database transports."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from test_mcp_integration import make_config
from test_operation_authority_fence import prepared

from app.api.admin_config import set_runtime_admin_token
from app.auth import AuthService
from app.config import DatabaseConfigStore
from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, Database, ModelCostRecord, TaskRunRecord
from app.harness.budget import tool_budget_scope
from app.harness.run_trace import run_trace_scope
from app.integrations.mcp import McpManager
from app.main import create_app
from app.runs import operation as operation_module
from app.runs.resources import RunToolBudget, resource_usage
from app.runs.trace_sources import capture_disabled_trace
from app.runs.unit_costs import settle_unit_cost


@asynccontextmanager
async def fixture(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    owned: bool = True,
    priced: bool = False,
    limit: int = 64,
    enabled: bool = True,
    cap: float = 1,
    ceiling: str = ".002",
    paginate: bool = False,
    write: bool = False,
    remote_error: bool = False,
    closed: asyncio.Event | None = None,
    on_request: Callable[[str, DatabaseConfigStore], Awaitable[None]] | None = None,
) -> AsyncIterator[tuple[AsyncClient, McpManager, DatabaseConfigStore, Database, list[str]]]:
    storage = await prepared(backend, tmp_path)
    set_runtime_admin_token(None)
    monkeypatch.setenv("NO_PROXY", "*")
    requests: list[str] = []
    try:
        if owned:
            await AuthService(storage.database).setup(
                display_name="Synthetic owner", password="synthetic secure password"
            )
        value = make_config(allow_write=True).model_dump(mode="json")
        value["run_budget"] = RunBudgetConfig(
            enabled=enabled,
            max_daily_cost=cap if enabled else None,
            cost_currency="CNY",
            max_tool_attempts=limit,
        ).model_dump(mode="json")
        if priced:
            value["mcp"]["servers"][0].update(
                catalog_refresh_cost={"cost_currency": "CNY", "request_cost_ceiling": ceiling},
                tool_call_cost={"cost_currency": "CNY", "request_cost_ceiling": ".003"},
            )
        path = tmp_path / "mcp-owned.yaml"
        path.write_text(yaml.safe_dump(value))
        store = DatabaseConfigStore(storage.database, path)
        await store.load()

        async def respond(request: httpx2.Request) -> httpx2.Response:
            message = json.loads(request.content) if request.method == "POST" else {}
            method = message.get("method", request.method)
            requests.append(method)
            if on_request is not None:
                await on_request(method, store)
            if request.method == "GET":
                return httpx2.Response(405)
            if request.method == "DELETE":
                return httpx2.Response(204)
            if "id" not in message:
                return httpx2.Response(202)
            if method == "initialize":
                result = {
                    "protocolVersion": message["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "Synthetic MCP", "version": "1"},
                }
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": "search",
                            "inputSchema": {"type": "object"},
                            "annotations": {"readOnlyHint": True},
                        }
                    ]
                }
                if write:
                    result["tools"].append(
                        {
                            "name": "create",
                            "inputSchema": {"type": "object"},
                            "annotations": {"readOnlyHint": False},
                        }
                    )
                if paginate and not message.get("params", {}).get("cursor"):
                    result["nextCursor"] = "synthetic-next"
                elif paginate:
                    result["tools"] = []
            else:
                result = {
                    "content": [{"type": "text", "text": "synthetic result"}],
                    "isError": remote_error,
                }
            return httpx2.Response(
                200,
                headers={"mcp-session-id": "synthetic-session"},
                json={"jsonrpc": "2.0", "id": message["id"], "result": result},
            )

        class Transport(httpx2.MockTransport):
            async def aclose(self) -> None:
                if closed is not None:
                    await asyncio.sleep(0.35)
                    closed.set()
                await super().aclose()

        monkeypatch.setattr(httpx2, "AsyncHTTPTransport", lambda **kwargs: Transport(respond))
        monkeypatch.setattr(
            "httpx2._client.AsyncHTTPTransport", lambda **kwargs: Transport(respond)
        )
        app = create_app(
            storage.database,
            config_store=store,
            watch_config=False,
            admin_token="synthetic-admin",
            run_dispatcher=False,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer synthetic-admin"},
        ) as client:
            yield client, app.state.mcp_manager, store, storage.database, requests
    finally:
        set_runtime_admin_token(None)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_native_refresh_missing_quote_stops_before_http(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with fixture(backend, tmp_path, monkeypatch) as (client, manager, _, database, requests):
        response = await client.post("/api/v1/admin/mcp/servers/books/refresh")
        assert requests == []
        assert response.status_code == 409
        assert response.json()["detail"] == "admin_cost_estimate_unavailable"
        assert requests == [] and manager.catalog() == ()
        async with database.sessions() as sql:
            assert not list(await sql.scalars(select(TaskRunRecord)))
            assert not list(await sql.scalars(select(ModelCostRecord)))


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_native_refresh_counts_all_http_and_keeps_workflow_quote(
    backend: str, enabled: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, priced=True, enabled=enabled) as (
        client,
        manager,
        _,
        database,
        requests,
    ):
        response = await client.post("/api/v1/admin/mcp/servers/books/refresh")
        assert response.status_code == 200, response.text
        assert response.json()["available"] and len(manager.catalog()) == 1
        assert "initialize" in requests and "tools/list" in requests and "DELETE" in requests
        async with database.sessions() as sql:
            runs = list(await sql.scalars(select(TaskRunRecord)))
            costs = list(await sql.scalars(select(ModelCostRecord)))
        assert len(runs) == len(costs) == 1
        assert runs[0].contract["entry"] == "mcp.refresh" and runs[0].status == "succeeded"
        assert resource_usage(runs[0]).get("tool_attempts", 0) == (len(requests) if enabled else 0)
        assert costs[0].call_id == runs[0].id and costs[0].state == "unknown"
        assert costs[0].unit_maximum_quantity == 1 and costs[0].unit_rate == Decimal(".002")
        assert costs[0].charged_micros == 2000


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_native_refresh_requires_actual_setup_account(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, owned=False, priced=True) as (
        client,
        _,
        _,
        database,
        requests,
    ):
        response = await client.post("/api/v1/admin/mcp/servers/books/refresh")
        assert (
            response.status_code == 409
            and response.json()["detail"] == "admin_account_setup_required"
        )
        assert requests == []
        async with database.sessions() as sql:
            assert not list(await sql.scalars(select(TaskRunRecord)))


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("limit", [1, 5])
async def test_native_refresh_attempt_limit_cannot_publish_partial_catalogue(
    backend: str, limit: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, priced=True, limit=limit) as (
        client,
        manager,
        _,
        database,
        requests,
    ):
        response = await client.post("/api/v1/admin/mcp/servers/books/refresh")
        assert response.status_code == 409, response.text
        assert len(requests) == limit and manager.catalog() == ()
        async with database.sessions() as sql:
            run = (await sql.scalars(select(TaskRunRecord))).one()
            cost = (await sql.scalars(select(ModelCostRecord))).one()
        assert run.status == "failed" and resource_usage(run)["tool_attempts"] == limit
        assert cost.state == "unknown" and cost.charged_micros == 2000


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_native_read_inherits_completed_root_and_original_trace(
    backend: str, enabled: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, priced=True, enabled=enabled) as (
        client,
        manager,
        _,
        database,
        requests,
    ):
        assert (await client.post("/api/v1/admin/mcp/servers/books/refresh")).json()["available"]
        async with database.sessions.begin() as sql:
            original = (await sql.scalars(select(TaskRunRecord))).one()
            original.deadline = datetime.now(UTC) - timedelta(seconds=1)
        deadline = datetime.now(UTC) + timedelta(seconds=30)
        tool = (
            RunToolBudget(
                database,
                run_id=original.id,
                user_id=original.user_id,
                config=RunBudgetConfig.model_validate(original.budget),
                maintenance=True,
                deadline=deadline,
            )
            if enabled
            else None
        )
        trace = (
            await capture_disabled_trace(
                database, original.id, original.user_id, maintenance=True, expires_at=deadline
            )
            if not enabled
            else None
        )
        with tool_budget_scope(tool), run_trace_scope(trace):
            result = await manager.call("mcp.books.search", {})
        assert result.ok, result.reason_code
        async with database.sessions() as sql:
            runs = list(await sql.scalars(select(TaskRunRecord)))
            costs = list(await sql.scalars(select(ModelCostRecord)))
        assert len(runs) == len(costs) == 2
        root = next(row for row in runs if row.id == original.id)
        child = next(row for row in runs if row.id != original.id)
        assert child.parent_run_id == root.id and child.contract["entry"] == "mcp.read"
        assert root.status == child.status == "succeeded"
        assert resource_usage(root).get("tool_attempts", 0) == (len(requests) if enabled else 0)
        assert resource_usage(child).get("tool_attempts", 0) == 0
        assert sum(row.charged_micros or 0 for row in costs) == 5000
        assert all(row.state == "unknown" for row in costs)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_background_refresh_counts_pages_without_fabricated_actor(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, priced=True, paginate=True) as (
        _,
        manager,
        _,
        database,
        requests,
    ):
        await manager.refresh_expired()
        assert len(manager.catalog()) == 1 and requests.count("tools/list") == 2
        async with database.sessions() as sql:
            run = (await sql.scalars(select(TaskRunRecord))).one()
        assert run.status == "succeeded" and resource_usage(run)["tool_attempts"] == len(requests)
        assert "source_actor" not in run.contract


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("ceiling", ["0", ".002"])
async def test_explicit_free_quote_differs_from_missing_quote_at_zero_money_limit(
    backend: str, ceiling: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, priced=True, cap=0, ceiling=ceiling) as (
        client,
        manager,
        _,
        database,
        requests,
    ):
        response = await client.post("/api/v1/admin/mcp/servers/books/refresh")
        if ceiling == "0":
            assert response.status_code == 200 and manager.catalog() and requests
            async with database.sessions() as sql:
                cost = (await sql.scalars(select(ModelCostRecord))).one()
            assert cost.state == "unknown" and cost.charged_micros == 0
        else:
            assert response.status_code == 409 and not requests and not manager.catalog()
            async with database.sessions() as sql:
                assert not list(await sql.scalars(select(TaskRunRecord)))
                assert not list(await sql.scalars(select(ModelCostRecord)))


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("revoke", [False, True])
async def test_catalogue_waits_for_terminal_cost_and_credential_authority(
    backend: str, revoke: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    original_settle = settle_unit_cost

    async def blocked(*args: object, **kwargs: object) -> None:
        entered.set()
        await release.wait()
        await original_settle(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(operation_module, "settle_unit_cost", blocked)
    async with fixture(backend, tmp_path, monkeypatch, priced=True) as (
        client,
        manager,
        _,
        database,
        requests,
    ):
        pending = asyncio.create_task(client.post("/api/v1/admin/mcp/servers/books/refresh"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert requests[-1] == "DELETE" and manager.catalog() == ()
            if revoke:
                set_runtime_admin_token("rotated-synthetic-admin")
            release.set()
            response = await pending
            assert bool(manager.catalog()) is not revoke
            assert response.status_code == (409 if revoke else 200)
            async with database.sessions() as sql:
                run = (await sql.scalars(select(TaskRunRecord))).one()
                cost = (await sql.scalars(select(ModelCostRecord))).one()
            assert run.status == ("failed" if revoke else "succeeded")
            assert cost.state == "unknown" and cost.charged_micros == 2000
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", ["disabled", "rollback", "credential"])
async def test_native_refresh_rejects_actual_source_changes_after_reply(
    backend: str, change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import HubConfig

    async def withdraw(method: str, store: DatabaseConfigStore) -> None:
        if method != "tools/list":
            return
        if change == "credential":
            set_runtime_admin_token("rotated-synthetic-admin")
            return
        original = store.current.version
        value = store.current.config.model_dump(mode="python")
        value["mcp"]["enabled"] = False
        draft = await store.create_draft(HubConfig.model_validate(value), actor="synthetic-admin")
        await store.publish(draft.version, actor="synthetic-admin")
        if change == "rollback":
            await store.rollback(original, actor="synthetic-admin")

    async with fixture(backend, tmp_path, monkeypatch, priced=True, on_request=withdraw) as (
        client,
        manager,
        _,
        database,
        requests,
    ):
        response = await client.post("/api/v1/admin/mcp/servers/books/refresh")
        assert response.status_code == 409 and not manager.catalog()
        assert "tools/list" in requests
        async with database.sessions() as sql:
            run = (await sql.scalars(select(TaskRunRecord))).one()
            cost = (await sql.scalars(select(ModelCostRecord))).one()
        assert run.status == "failed" and cost.state == "unknown"
        assert cost.charged_micros == 2000


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_sql_owner_revocation_during_native_shutdown_waits_for_same_task_close(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = asyncio.Event()

    async def revoke(method: str, _store: DatabaseConfigStore) -> None:
        if method == "DELETE":
            async with database.sessions.begin() as sql:
                owner = (await sql.scalars(select(AppUserRecord))).one()
                owner.status = "inactive"

    async with fixture(
        backend, tmp_path, monkeypatch, priced=True, closed=closed, on_request=revoke
    ) as (client, manager, _, database, requests):
        response = await client.post("/api/v1/admin/mcp/servers/books/refresh")
        assert response.status_code == 409 and closed.is_set() and not manager.catalog()
        assert requests[-1] == "DELETE"
        async with database.sessions() as sql:
            run = (await sql.scalars(select(TaskRunRecord))).one()
        assert run.status == "failed"


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("remote_error", [False, True])
async def test_native_write_records_real_attempts_and_remote_error_outcome(
    backend: str, remote_error: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with fixture(
        backend, tmp_path, monkeypatch, priced=True, write=True, remote_error=remote_error
    ) as (client, manager, _, database, requests):
        assert (await client.post("/api/v1/admin/mcp/servers/books/refresh")).json()["available"]
        attempts_before = len(requests)
        async with database.sessions() as sql:
            owner = (await sql.scalars(select(TaskRunRecord.user_id))).one()
        result = await manager.call_write("mcp.books.create", {}, user_id=owner)
        assert result.ok is not remote_error
        if remote_error:
            assert result.reason_code == "mcp_remote_tool_error"
        async with database.sessions() as sql:
            run = (
                await sql.scalars(
                    select(TaskRunRecord).where(
                        TaskRunRecord.contract["entry"].as_string() == "mcp.write"
                    )
                )
            ).one()
            cost = await sql.get_one(ModelCostRecord, run.id)
        assert run.status == ("failed" if remote_error else "succeeded")
        assert resource_usage(run)["tool_attempts"] == len(requests) - attempts_before
        assert requests.count("tools/call") == 1
        assert cost.state == "unknown" and cost.charged_micros == 3000


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("write", [False, True])
async def test_anonymous_native_call_cannot_borrow_background_refresh_account(
    backend: str, write: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, priced=True, write=True) as (
        client,
        manager,
        _,
        database,
        requests,
    ):
        assert (await client.post("/api/v1/admin/mcp/servers/books/refresh")).json()["available"]
        before = len(requests)
        result = (
            await manager.call_write("mcp.books.create", {})
            if write
            else await manager.call("mcp.books.search", {})
        )
        assert not result.ok and result.reason_code == "mcp_request_owner_missing"
        assert len(requests) == before
        async with database.sessions() as sql:
            assert len(list(await sql.scalars(select(TaskRunRecord)))) == 1
            assert len(list(await sql.scalars(select(ModelCostRecord)))) == 1


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_native_explicit_owner_cannot_replace_inherited_wallet_owner(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ids import uuid7

    async with fixture(backend, tmp_path, monkeypatch, priced=True) as (
        client,
        manager,
        _,
        database,
        requests,
    ):
        assert (await client.post("/api/v1/admin/mcp/servers/books/refresh")).json()["available"]
        async with database.sessions.begin() as sql:
            root = (await sql.scalars(select(TaskRunRecord))).one()
            other = uuid7()
            sql.add(AppUserRecord(id=other, display_name="Other synthetic owner", status="active"))
        budget = RunToolBudget(
            database,
            run_id=root.id,
            user_id=root.user_id,
            config=RunBudgetConfig.model_validate(root.budget),
            maintenance=True,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
        before = len(requests)
        with tool_budget_scope(budget):
            result = await manager.call("mcp.books.search", {}, user_id=other)
        assert not result.ok and result.reason_code == "budget_owner_invalid"
        assert len(requests) == before
        async with database.sessions() as sql:
            assert len(list(await sql.scalars(select(TaskRunRecord)))) == 1


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_native_manager_without_owned_adapter_never_opens_connection(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.integrations.mcp import McpManagerError

    async with fixture(backend, tmp_path, monkeypatch, priced=True) as (
        _,
        _,
        store,
        database,
        requests,
    ):
        manager = McpManager(store)
        with pytest.raises(McpManagerError, match="mcp_operation_owner_missing"):
            await manager.refresh_server("books")
        assert not requests and not manager.catalog()
        async with database.sessions() as sql:
            assert not list(await sql.scalars(select(TaskRunRecord)))
