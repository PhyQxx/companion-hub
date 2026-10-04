"""Owned native sync with isolated SQL and synthetic HTTP responses."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import yaml
from fastapi import FastAPI
from sqlalchemy import select
from test_calendar import _service
from test_calendar_caldav import PROPFIND_RESPONSE, SINGLE_EVENT, _report_response
from test_database_config import config_yaml
from test_operation_authority_fence import prepared

from app.api.admin_config import set_runtime_admin_token
from app.api.calendar import create_calendar_router
from app.auth import AuthService
from app.calendar.caldav import CalDavClient, CalDavSyncService
from app.calendar.google import GoogleCalendarClient, GoogleCalendarSyncService, GoogleTokenStore
from app.calendar.tools import CalendarSyncArgs, CalendarSyncTool
from app.config import ConfigStore, DatabaseConfigStore
from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, CalendarEventRecord, Database, ModelCostRecord, TaskRunRecord
from app.harness.budget import BudgetDenied, tool_budget_scope
from app.harness.run_trace import run_trace_scope
from app.ids import uuid7
from app.main import create_app
from app.runs import operation
from app.runs.resources import RunToolBudget, resource_usage
from app.runs.trace_sources import capture_disabled_trace
from app.runs.unit_costs import settle_unit_cost
from app.tools.contracts import ToolContext


@asynccontextmanager
async def fixture(
    backend: str,
    provider: str,
    tmp_path: Path,
    *,
    priced: bool = False,
    enabled: bool = True,
    limit: int = 64,
    cap: float = 1,
    ceiling: str = ".002",
    remote_error: bool = False,
    redirect: bool = False,
    foreign_collection: bool = False,
) -> AsyncIterator[
    tuple[
        CalDavSyncService | GoogleCalendarSyncService,
        ConfigStore,
        Database,
        UUID,
        list[str],
        list[httpx.AsyncClient],
    ]
]:
    storage = await prepared(backend, tmp_path)
    requests: list[str] = []
    clients: list[httpx.AsyncClient] = []
    try:
        account = await AuthService(storage.database).setup(
            display_name="Synthetic calendar owner", password="synthetic calendar password"
        )
        owner = account.principal.user_id
        value = yaml.safe_load(config_yaml())
        value["run_budget"] = RunBudgetConfig(
            enabled=enabled,
            max_tool_attempts=limit,
            cost_currency="CNY",
            max_daily_cost=cap if enabled else None,
        ).model_dump(mode="json")
        connection = {
            "enabled": True,
            "secret_value": "synthetic-calendar-secret",
            "window_days_back": 30,
            **(
                {"url": "https://calendar.example.test/", "username": "synthetic"}
                if provider == "caldav"
                else {"client_id": "synthetic-calendar-client", "calendar_ids": ["primary"]}
            ),
        }
        if priced:
            connection["sync_cost"] = {"cost_currency": "CNY", "request_cost_ceiling": ceiling}
        value["integrations"] = {"calendar": {provider: connection}}
        path = tmp_path / "owned-calendar.yaml"
        path.write_text(yaml.safe_dump(value))
        store = ConfigStore(path)
        await store.load()

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request.method)
            if redirect:
                return httpx.Response(
                    307, headers={"Location": "https://other.example.test/redirect"}
                )
            if remote_error:
                return httpx.Response(503)
            if provider == "caldav":
                return httpx.Response(
                    207,
                    text=PROPFIND_RESPONSE.replace(
                        "/calendars/personal/", "https://other.example.test/calendar/"
                    )
                    if foreign_collection and request.method == "PROPFIND"
                    else PROPFIND_RESPONSE
                    if request.method == "PROPFIND"
                    else _report_response([("synthetic-etag", SINGLE_EVENT)]),
                )
            if request.method == "POST":
                return httpx.Response(
                    200, json={"access_token": "synthetic-access", "expires_in": 3600}
                )
            if "pageToken" in request.url.params:
                return httpx.Response(200, json={"items": []})
            moment = datetime.now(UTC)
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "synthetic-event",
                            "summary": "Synthetic calendar event",
                            "start": {"dateTime": moment.isoformat()},
                            "end": {"dateTime": (moment + timedelta(hours=1)).isoformat()},
                        }
                    ],
                    "nextPageToken": "synthetic-next",
                },
            )

        def caldav(**kwargs: object) -> CalDavClient:
            http = httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True)
            clients.append(http)
            client = CalDavClient(**kwargs, client=http)  # type: ignore[arg-type]
            client._owns_client = True
            return client

        def google(**kwargs: object) -> GoogleCalendarClient:
            http = httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True)
            clients.append(http)
            client = GoogleCalendarClient(**kwargs, client=http)  # type: ignore[arg-type]
            client._owns_client = True
            return client

        if provider == "google":
            await GoogleTokenStore(storage.database).save(owner, "synthetic-refresh", None)
            service: CalDavSyncService | GoogleCalendarSyncService = GoogleCalendarSyncService(
                storage.database, store, client_factory=google
            )
        else:
            service = CalDavSyncService(storage.database, store, client_factory=caldav)
        yield service, store, storage.database, owner, requests, clients
    finally:
        for http in clients:
            await http.aclose()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
async def test_missing_sync_quote_prevents_native_requests_and_mirror_writes(
    backend: str, provider: str, tmp_path: Path
) -> None:
    async with fixture(backend, provider, tmp_path) as (
        service,
        _,
        database,
        owner,
        requests,
        clients,
    ):
        with pytest.raises(BudgetDenied):
            await service.sync_once(user_id=owner)
        assert not requests and not clients
        async with database.sessions() as sql:
            assert not list(await sql.scalars(select(CalendarEventRecord)))
            assert not list(await sql.scalars(select(TaskRunRecord)))
            assert not list(await sql.scalars(select(ModelCostRecord)))


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_native_sync_owns_actual_requests_and_atomic_mirrors(
    backend: str, provider: str, enabled: bool, tmp_path: Path
) -> None:
    async with fixture(backend, provider, tmp_path, priced=True, enabled=enabled) as (
        service,
        _,
        database,
        owner,
        requests,
        clients,
    ):
        stats = await service.sync_once(user_id=owner)
        assert not stats.errors and stats.mirrors_created == 1
        assert requests == (
            ["PROPFIND", "REPORT"] if provider == "caldav" else ["POST", "GET", "GET"]
        )
        assert all(http.is_closed for http in clients)
        async with database.sessions() as sql:
            run = (await sql.scalars(select(TaskRunRecord))).one()
            cost = (await sql.scalars(select(ModelCostRecord))).one()
            event = (await sql.scalars(select(CalendarEventRecord))).one()
        assert run.user_id == event.user_id == cost.user_id == owner
        assert run.status == "succeeded" and run.contract["calendar_mirror_committed"] == "true"
        assert run.contract["calendar_snapshot_complete"] == "true"
        assert resource_usage(run).get("tool_attempts", 0) == (len(requests) if enabled else 0)
        assert cost.call_id == run.id and cost.state == "unknown" and cost.charged_micros == 2000
        assert event.title not in str(run.contract) and "synthetic-calendar-secret" not in str(
            run.contract
        )


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
async def test_native_sync_request_limit_stops_before_next_send_and_rolls_back(
    backend: str, provider: str, tmp_path: Path
) -> None:
    async with fixture(backend, provider, tmp_path, priced=True, limit=1) as (
        service,
        _,
        database,
        owner,
        requests,
        clients,
    ):
        with pytest.raises(BudgetDenied):
            await service.sync_once(user_id=owner)
        assert len(requests) == 1 and all(http.is_closed for http in clients)
        async with database.sessions() as sql:
            run = (await sql.scalars(select(TaskRunRecord))).one()
            cost = (await sql.scalars(select(ModelCostRecord))).one()
            assert not list(await sql.scalars(select(CalendarEventRecord)))
        assert run.status == "failed" and resource_usage(run)["tool_attempts"] == 1
        assert cost.state == "unknown" and cost.charged_micros == 2000


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
async def test_terminal_fee_failure_rolls_back_mirror_and_success_evidence(
    backend: str, provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def deny(*args: Any, **kwargs: Any) -> None:
        sql = args[0]
        assert (await sql.scalars(select(CalendarEventRecord))).one()
        raise BudgetDenied("unit_cost_reservation_inactive")

    monkeypatch.setattr(operation, "settle_unit_cost", deny)
    async with fixture(backend, provider, tmp_path, priced=True) as (
        service,
        _,
        database,
        owner,
        requests,
        clients,
    ):
        with pytest.raises(BudgetDenied, match="unit_cost_reservation_inactive"):
            await service.sync_once(user_id=owner)
        assert requests and all(http.is_closed for http in clients)
        async with database.sessions() as sql:
            run = (await sql.scalars(select(TaskRunRecord))).one()
            cost = (await sql.scalars(select(ModelCostRecord))).one()
            assert not list(await sql.scalars(select(CalendarEventRecord)))
        assert run.status == "failed" and "calendar_mirror_committed" not in run.contract
        assert cost.state == "unknown" and cost.charged_micros == 2000


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
async def test_all_remote_fetches_failed_preserves_failed_run_and_unknown_fee(
    backend: str, provider: str, tmp_path: Path
) -> None:
    async with fixture(backend, provider, tmp_path, priced=True, remote_error=True) as (
        service,
        _,
        database,
        owner,
        requests,
        clients,
    ):
        stats = await service.sync_once(user_id=owner)
        assert stats.errors and stats.mirrors_created == 0 and len(requests) == 1
        assert all(http.is_closed for http in clients)
        async with database.sessions() as sql:
            run = (await sql.scalars(select(TaskRunRecord))).one()
            cost = (await sql.scalars(select(ModelCostRecord))).one()
            assert not list(await sql.scalars(select(CalendarEventRecord)))
        assert run.status == "failed" and "calendar_mirror_committed" not in run.contract
        assert cost.state == "unknown" and cost.charged_micros == 2000


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
@pytest.mark.parametrize("ceiling", ["0", ".002"])
async def test_explicit_free_sync_quote_differs_from_unknown_at_zero_cap(
    backend: str, provider: str, ceiling: str, tmp_path: Path
) -> None:
    async with fixture(backend, provider, tmp_path, priced=True, cap=0, ceiling=ceiling) as (
        service,
        _,
        database,
        owner,
        requests,
        clients,
    ):
        if ceiling == "0":
            assert not (await service.sync_once(user_id=owner)).errors
            async with database.sessions() as sql:
                cost = (await sql.scalars(select(ModelCostRecord))).one()
            assert cost.charged_micros == 0 and cost.state == "unknown"
        else:
            with pytest.raises(BudgetDenied):
                await service.sync_once(user_id=owner)
            assert not requests and not clients
            async with database.sessions() as sql:
                assert not list(await sql.scalars(select(TaskRunRecord)))


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_sync_inherits_existing_completed_root_and_charges_original_quota(
    backend: str, provider: str, enabled: bool, tmp_path: Path
) -> None:
    async with fixture(backend, provider, tmp_path, priced=True, enabled=enabled) as (
        service,
        _,
        database,
        owner,
        requests,
        _,
    ):
        await service.sync_once(user_id=owner)
        async with database.sessions.begin() as sql:
            root = (await sql.scalars(select(TaskRunRecord))).one()
            root.deadline = datetime.now(UTC) - timedelta(seconds=1)
        deadline = datetime.now(UTC) + timedelta(seconds=30)
        tool = (
            RunToolBudget(
                database,
                run_id=root.id,
                user_id=owner,
                config=RunBudgetConfig.model_validate(root.budget),
                maintenance=True,
                deadline=deadline,
            )
            if enabled
            else None
        )
        trace = (
            await capture_disabled_trace(
                database,
                root.id,
                owner,
                maintenance=True,
                expires_at=deadline,
            )
            if not enabled
            else None
        )
        with tool_budget_scope(tool), run_trace_scope(trace):
            stats = await service.sync_once(user_id=owner)
        assert not stats.errors
        async with database.sessions() as sql:
            runs = list(await sql.scalars(select(TaskRunRecord)))
            costs = list(await sql.scalars(select(ModelCostRecord)))
        child = next(row for row in runs if row.id != root.id)
        current_root = next(row for row in runs if row.id == root.id)
        assert child.parent_run_id == root.id and child.status == current_root.status == "succeeded"
        assert resource_usage(current_root).get("tool_attempts", 0) == (
            len(requests) if enabled else 0
        )
        assert resource_usage(child).get("tool_attempts", 0) == 0
        assert len(costs) == 2 and sum(row.charged_micros or 0 for row in costs) == 4000


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
async def test_native_redirect_cannot_send_an_unadmitted_hidden_request(
    backend: str, provider: str, tmp_path: Path
) -> None:
    async with fixture(backend, provider, tmp_path, priced=True, redirect=True) as (
        service,
        _,
        database,
        owner,
        requests,
        clients,
    ):
        assert (await service.sync_once(user_id=owner)).errors
        assert len(requests) == 1 and all(http.is_closed for http in clients)
        async with database.sessions() as sql:
            assert (await sql.scalars(select(TaskRunRecord))).one().status == "failed"


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_caldav_discovered_foreign_origin_cannot_receive_connection_secret(
    backend: str, tmp_path: Path
) -> None:
    async with fixture(backend, "caldav", tmp_path, priced=True, foreign_collection=True) as (
        service,
        _,
        database,
        owner,
        requests,
        _,
    ):
        stats = await service.sync_once(user_id=owner)
        assert stats.errors and "caldav_source_origin_invalid" in stats.errors[0]
        assert requests == ["PROPFIND"]
        async with database.sessions() as sql:
            run = (await sql.scalars(select(TaskRunRecord))).one()
        assert run.status == "failed" and resource_usage(run)["tool_attempts"] == 1


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_background_sync_resolves_actual_owner_without_invented_actor(
    backend: str, provider: str, enabled: bool, tmp_path: Path
) -> None:
    async with fixture(backend, provider, tmp_path, priced=True, enabled=enabled) as (
        service,
        _,
        database,
        owner,
        requests,
        _,
    ):
        assert not (await service.sync_once()).errors
        async with database.sessions() as sql:
            run = (await sql.scalars(select(TaskRunRecord))).one()
        assert requests and run.user_id == owner and run.status == "succeeded"
        assert "source_actor" not in run.contract


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
@pytest.mark.parametrize("revoke", [False, True])
async def test_production_admin_sync_wiring_fences_bearer_through_fee_commit(
    backend: str, provider: str, revoke: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    original_settle = settle_unit_cost

    async def blocked(*args: Any, **kwargs: Any) -> None:
        entered.set()
        await release.wait()
        await original_settle(*args, **kwargs)

    monkeypatch.setattr(operation, "settle_unit_cost", blocked)
    async with fixture(backend, provider, tmp_path, priced=True) as (
        service,
        config,
        database,
        owner,
        requests,
        clients,
    ):
        original_init = type(service).__init__

        def initialize(self: Any, *args: Any, **kwargs: Any) -> None:
            original_init(self, *args, **{**kwargs, "client_factory": service._client_factory})

        monkeypatch.setattr(type(service), "__init__", initialize)
        store = DatabaseConfigStore(database, config.path)
        await store.load()
        app = create_app(
            database, config_store=store, watch_config=False, admin_token="synthetic-calendar-admin"
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as http:
                path = f"/api/v1/admin/config/integrations/calendar/{provider}/sync"
                assert (await http.post(path)).status_code == 401 and not requests
                pending = asyncio.create_task(
                    http.post(
                        path,
                        headers={
                            "Authorization": "Bearer synthetic-calendar-admin",
                        },
                    )
                )
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    assert requests and all(client.is_closed for client in clients)
                    if revoke:
                        set_runtime_admin_token("rotated-synthetic-calendar-admin")
                    release.set()
                    response = await pending
                    assert response.status_code == (409 if revoke else 200), response.text
                    async with database.sessions() as sql:
                        run = (await sql.scalars(select(TaskRunRecord))).one()
                        cost = (await sql.scalars(select(ModelCostRecord))).one()
                        mirrors = list(await sql.scalars(select(CalendarEventRecord)))
                    assert run.user_id == owner and run.status == (
                        "failed" if revoke else "succeeded"
                    )
                    assert bool(mirrors) is not revoke
                    assert cost.state == "unknown" and cost.charged_micros == 2000
                finally:
                    release.set()
                    if not pending.done():
                        pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
        finally:
            set_runtime_admin_token(None)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
async def test_user_sync_api_reports_missing_quote_before_native_client_creation(
    backend: str, provider: str, tmp_path: Path
) -> None:
    async with fixture(backend, provider, tmp_path) as (
        service,
        _,
        database,
        _,
        requests,
        clients,
    ):
        auth = AuthService(database)
        actor = await auth.login(password="synthetic calendar password")
        app = FastAPI()
        app.include_router(
            create_calendar_router(
                _service(database, clock=lambda: datetime.now(UTC)),
                auth,
                caldav_sync=service if isinstance(service, CalDavSyncService) else None,
                google_sync=service if isinstance(service, GoogleCalendarSyncService) else None,
            )
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            response = await http.post(
                f"/api/v1/calendar/{provider}/sync",
                headers={"Authorization": f"Bearer {actor.access_token}"},
            )
        assert response.status_code == 409
        assert response.json()["detail"] == "admin_cost_estimate_unavailable"
        assert not requests and not clients


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_calendar_sync_cannot_borrow_foreign_completed_root(
    backend: str, provider: str, enabled: bool, tmp_path: Path
) -> None:
    async with fixture(backend, provider, tmp_path, priced=True, enabled=enabled) as (
        service,
        _,
        database,
        owner,
        requests,
        _,
    ):
        other = uuid7()
        async with database.sessions.begin() as sql:
            sql.add(AppUserRecord(id=other, display_name="Other synthetic owner", status="active"))
        if provider == "google":
            await GoogleTokenStore(database).save(other, "other-synthetic-refresh", None)
        await service.sync_once(user_id=other)
        sent = len(requests)
        async with database.sessions() as sql:
            root = (await sql.scalars(select(TaskRunRecord))).one()
        deadline = datetime.now(UTC) + timedelta(seconds=30)
        tool = (
            RunToolBudget(
                database,
                run_id=root.id,
                user_id=other,
                config=RunBudgetConfig.model_validate(root.budget),
                maintenance=True,
                deadline=deadline,
            )
            if enabled
            else None
        )
        trace = (
            await capture_disabled_trace(
                database, root.id, other, maintenance=True, expires_at=deadline
            )
            if not enabled
            else None
        )
        with tool_budget_scope(tool), run_trace_scope(trace), pytest.raises(BudgetDenied):
            await service.sync_once(user_id=owner)
        assert len(requests) == sent
        async with database.sessions() as sql:
            assert len(list(await sql.scalars(select(TaskRunRecord)))) == 1


@pytest.mark.parametrize(
    "denial", [BudgetDenied("budget_tool_limit"), PermissionError("calendar_sync_actor_invalid")]
)
async def test_all_provider_tool_does_not_turn_authority_denial_into_fallback(
    denial: Exception,
) -> None:
    calls: list[str] = []

    class First:
        async def sync_once(self, **kwargs: Any) -> None:
            calls.append("first")
            raise denial

    class Second:
        async def sync_once(self, **kwargs: Any) -> None:
            calls.append("second")

    tool = CalendarSyncTool(caldav_sync=First(), google_sync=Second())
    with pytest.raises(type(denial)):
        await tool.execute(
            CalendarSyncArgs(provider="all"),
            ToolContext(privacy_level="L1", user_id=uuid7(), turn_id=uuid7()),
        )
    assert calls == ["first"]
