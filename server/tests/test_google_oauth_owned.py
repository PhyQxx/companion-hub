"""Session-bound single-use OAuth callbacks use isolated SQL and synthetic providers."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import pytest
import yaml
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_calendar import _service
from test_database_config import config_yaml
from test_operation_authority_fence import prepared

from app.api.admin_config import set_runtime_admin_token
from app.api.calendar import create_calendar_router
from app.auth import AuthService
from app.calendar.google import GoogleCalendarSyncService, GoogleTokenStore
from app.config import ConfigStore, DatabaseConfigStore
from app.config.models import RunBudgetConfig
from app.db import (
    AppUserRecord,
    AuthSessionRecord,
    CalendarOAuthStateRecord,
    CalendarOAuthTokenRecord,
    Database,
    ModelCostRecord,
    TaskRunRecord,
)
from app.harness.budget import BudgetDenied, tool_budget_scope
from app.harness.run_trace import run_trace_scope
from app.ids import uuid7
from app.main import create_app
from app.runs import google_oauth as oauth_operation
from app.runs import operation
from app.runs.resources import RunToolBudget, resource_usage
from app.runs.trace_sources import capture_disabled_trace
from app.runs.unit_costs import settle_unit_cost


class Requests(list[str]):
    def __init__(self) -> None:
        super().__init__()
        self.clients: list[httpx.AsyncClient] = []


@asynccontextmanager
async def fixture(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool = False,
    priced: bool = False,
    ceiling: str = ".002",
    cap: float = 1,
    response_kind: str = "ok",
    close_entered: asyncio.Event | None = None,
    close_release: asyncio.Event | None = None,
    on_request: Callable[[Database, ConfigStore], Awaitable[None]] | None = None,
) -> AsyncIterator[
    tuple[httpx.AsyncClient, AuthService, str, UUID, ConfigStore, Database, Requests]
]:
    storage = await prepared(backend, tmp_path)
    calls = Requests()
    set_runtime_admin_token(None)
    try:
        auth = AuthService(storage.database)
        account = await auth.setup(
            display_name="Synthetic OAuth owner", password="synthetic OAuth password"
        )
        value = yaml.safe_load(config_yaml())
        value["run_budget"] = RunBudgetConfig(
            enabled=enabled, cost_currency="CNY", max_daily_cost=cap if enabled else None
        ).model_dump(mode="json")
        value["integrations"] = {
            "calendar": {
                "google": {
                    "enabled": True,
                    "client_id": "synthetic-oauth-client",
                    "secret_value": "synthetic-oauth-secret",
                    "redirect_uri": "https://hub.example.test/api/v1/calendar/google/callback",
                }
            }
        }
        if priced:
            value["integrations"]["calendar"]["google"]["code_exchange_cost"] = {
                "cost_currency": "CNY",
                "request_cost_ceiling": ceiling,
            }
        path = tmp_path / "oauth.yaml"
        path.write_text(yaml.safe_dump(value))
        store = ConfigStore(path)
        await store.load()

        async def respond(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if on_request is not None:
                await on_request(storage.database, store)
            if response_kind == "redirect":
                return httpx.Response(307, headers={"Location": "https://other.example.test/token"})
            if response_kind == "http_error":
                return httpx.Response(503)
            if response_kind == "invalid_json":
                return httpx.Response(200, text="not JSON")
            payload: object = {
                "ok": {"refresh_token": "synthetic-oauth-refresh"},
                "missing_refresh": {},
                "invalid_refresh": {"refresh_token": ["not-a-token"]},
                "invalid_shape": [],
            }[response_kind]
            return httpx.Response(200, json=payload)

        def http_factory(timeout_seconds: float) -> httpx.AsyncClient:
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(respond), follow_redirects=True
            )
            if close_entered is not None and close_release is not None:
                original_close = client.aclose

                async def close() -> None:
                    close_entered.set()
                    await close_release.wait()
                    await original_close()

                monkeypatch.setattr(client, "aclose", close)
            calls.clients.append(client)
            return client

        monkeypatch.setattr(oauth_operation, "new_oauth_http", http_factory)
        google = GoogleCalendarSyncService(storage.database, store)
        app = FastAPI()
        app.include_router(
            create_calendar_router(
                _service(storage.database, clock=lambda: datetime.now(UTC)),
                auth,
                google_sync=google,
                google_state_key="synthetic-oauth-state-key",
            )
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            yield (
                http,
                auth,
                account.access_token,
                account.principal.user_id,
                store,
                storage.database,
                calls,
            )
    finally:
        for client in calls.clients:
            await client.aclose()
        set_runtime_admin_token(None)
        await storage.close()


async def authorize(http: httpx.AsyncClient, token: str) -> str:
    response = await http.get(
        "/api/v1/calendar/google/authorize", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    return parse_qs(urlsplit(str(response.json()["authorize_url"])).query)["state"][0]


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_each_authorization_has_a_fresh_state(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch) as (http, _, token, _, _, _, _):
        assert await authorize(http, token) != await authorize(http, token)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_callback_uses_authorizing_session_owner_instead_of_default_user(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch) as (
        http,
        _,
        token,
        owner,
        _,
        database,
        calls,
    ):
        other = uuid7()
        async with database.sessions.begin() as sql:
            sql.add(
                AppUserRecord(
                    id=other,
                    display_name="Older synthetic owner",
                    status="active",
                    created_at=datetime.now(UTC) - timedelta(days=1),
                )
            )
        state = await authorize(http, token)
        response = await http.get(
            "/api/v1/calendar/google/callback", params={"state": state, "code": "synthetic-code"}
        )
        assert response.status_code == 200, response.text
        async with database.sessions() as sql:
            record = (await sql.scalars(select(CalendarOAuthTokenRecord))).one()
        assert record.user_id == owner and calls == ["POST"]


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_callback_state_is_consumed_before_provider_and_cannot_replay(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch) as (http, _, token, _, _, _, calls):
        state = await authorize(http, token)
        params = {"state": state, "code": "synthetic-code"}
        assert (
            await http.get("/api/v1/calendar/google/callback", params=params)
        ).status_code == 200
        response = await http.get("/api/v1/calendar/google/callback", params=params)
        assert response.status_code == 400 and calls == ["POST"]


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_native_code_exchange_has_owned_run_fee_request_and_atomic_token(
    backend: str,
    enabled: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, enabled=enabled, priced=True) as (
        http,
        _,
        token,
        owner,
        _,
        database,
        calls,
    ):
        state = await authorize(http, token)
        async with database.sessions() as sql:
            row = (await sql.scalars(select(CalendarOAuthStateRecord))).one()
        assert row.user_id == owner and row.consumed_at is None
        assert state not in str(row.__dict__) and "synthetic-oauth-secret" not in str(row.__dict__)
        response = await http.get(
            "/api/v1/calendar/google/callback", params={"state": state, "code": "synthetic-code"}
        )
        assert response.status_code == 200, response.text
        assert calls == ["POST"] and all(client.is_closed for client in calls.clients)
        async with database.sessions() as sql:
            run = (await sql.scalars(select(TaskRunRecord))).one()
            cost = (await sql.scalars(select(ModelCostRecord))).one()
            row = (await sql.scalars(select(CalendarOAuthStateRecord))).one()
            saved = (await sql.scalars(select(CalendarOAuthTokenRecord))).one()
        assert run.user_id == cost.user_id == saved.user_id == owner
        assert run.status == "succeeded" and run.contract["google_oauth_token_committed"] == "true"
        assert row.consumed_at is not None and row.completed_at is not None
        assert cost.call_id == run.id and cost.state == "unknown" and cost.charged_micros == 2000
        assert resource_usage(run).get("tool_attempts", 0) == (1 if enabled else 0)
        assert "synthetic-code" not in str(run.contract) and "synthetic-oauth-refresh" not in str(
            run.contract
        )


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_missing_exchange_quote_is_consumed_but_never_creates_http_client(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, enabled=True) as (
        http,
        _,
        token,
        _,
        _,
        database,
        calls,
    ):
        state = await authorize(http, token)
        params = {"state": state, "code": "synthetic-code"}
        response = await http.get("/api/v1/calendar/google/callback", params=params)
        assert (
            response.status_code == 409
            and response.json()["detail"] == "admin_cost_estimate_unavailable"
        )
        assert not calls and not calls.clients
        assert (
            await http.get("/api/v1/calendar/google/callback", params=params)
        ).status_code == 400
        async with database.sessions() as sql:
            assert (
                await sql.scalars(select(CalendarOAuthStateRecord))
            ).one().consumed_at is not None
            assert not list(await sql.scalars(select(TaskRunRecord)))
            assert not list(await sql.scalars(select(ModelCostRecord)))
            assert not list(await sql.scalars(select(CalendarOAuthTokenRecord)))


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("ceiling", ["0", ".002"])
async def test_zero_exchange_quote_differs_from_over_cap_quote(
    backend: str,
    ceiling: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(
        backend, tmp_path, monkeypatch, enabled=True, priced=True, cap=0, ceiling=ceiling
    ) as (
        http,
        _,
        token,
        _,
        _,
        database,
        calls,
    ):
        response = await http.get(
            "/api/v1/calendar/google/callback",
            params={"state": await authorize(http, token), "code": "synthetic-code"},
        )
        assert response.status_code == (200 if ceiling == "0" else 409), response.text
        if ceiling == "0":
            async with database.sessions() as sql:
                cost = (await sql.scalars(select(ModelCostRecord))).one()
            assert cost.state == "unknown" and cost.charged_micros == 0
        else:
            assert not calls and not calls.clients


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_simultaneous_callbacks_consume_once_before_native_http(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, priced=True) as (
        http,
        _,
        token,
        _,
        _,
        database,
        calls,
    ):
        params = {"state": await authorize(http, token), "code": "synthetic-code"}
        responses = await asyncio.gather(
            *(http.get("/api/v1/calendar/google/callback", params=params) for _ in range(4))
        )
        assert sorted(response.status_code for response in responses) == [200, 400, 400, 400]
        assert calls == ["POST"]
        async with database.sessions() as sql:
            assert len(list(await sql.scalars(select(TaskRunRecord)))) == 1


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize(
    "change",
    [
        "logout",
        "expire",
        "owner",
        "disable",
        "rollback",
        "env_rotate",
        "env_remove",
        "key",
        "reauthorize",
        "disconnect",
    ],
)
async def test_changed_authority_before_callback_prevents_http(
    backend: str,
    change: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, priced=True) as (
        http,
        auth,
        token,
        owner,
        store,
        database,
        calls,
    ):
        if change.startswith("env_"):
            monkeypatch.setenv("GOOGLE_OAUTH_FIXTURE_SECRET", "synthetic-oauth-secret")
            value = yaml.safe_load(store.path.read_text())
            config = value["integrations"]["calendar"]["google"]
            del config["secret_value"]
            config["secret_ref"] = "env:GOOGLE_OAUTH_FIXTURE_SECRET"
            store.path.write_text(yaml.safe_dump(value))
            await store.reload()
        state = await authorize(http, token)
        principal = await auth.authenticate(token)
        if change == "logout":
            await auth.logout(principal)
        elif change == "expire":
            async with database.sessions.begin() as sql:
                auth_row = await sql.get_one(AuthSessionRecord, principal.session_id)
                auth_row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif change == "owner":
            async with database.sessions.begin() as sql:
                owner_row = await sql.get_one(AppUserRecord, owner)
                owner_row.status = "inactive"
        elif change in {"disable", "rollback"}:
            original = store.path.read_text()
            value = yaml.safe_load(original)
            value["integrations"]["calendar"]["google"]["enabled"] = False
            store.path.write_text(yaml.safe_dump(value))
            await store.reload()
            if change == "rollback":
                store.path.write_text(original)
                await store.reload()
        elif change == "env_rotate":
            monkeypatch.setenv("GOOGLE_OAUTH_FIXTURE_SECRET", "rotated-oauth-secret")
        elif change == "env_remove":
            monkeypatch.delenv("GOOGLE_OAUTH_FIXTURE_SECRET")
        elif change == "key":
            set_runtime_admin_token("rotated-synthetic-state-key")
        elif change == "reauthorize":
            await authorize(http, token)
        else:
            assert (
                await http.delete(
                    "/api/v1/calendar/google/token", headers={"Authorization": f"Bearer {token}"}
                )
            ).json() == {"removed": False}
        response = await http.get(
            "/api/v1/calendar/google/callback", params={"state": state, "code": "synthetic-code"}
        )
        assert response.status_code in {400, 409}, response.text
        assert not calls and not calls.clients
        async with database.sessions() as sql:
            assert not list(await sql.scalars(select(CalendarOAuthTokenRecord)))
            assert not list(await sql.scalars(select(TaskRunRecord)))


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize(
    "change", ["logout", "owner", "disconnect", "reauthorize", "rollback", "key", "state_expire"]
)
async def test_changed_authority_during_exchange_keeps_unknown_fee_without_token(
    backend: str,
    change: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait(database: Database, store: ConfigStore) -> None:
        entered.set()
        await release.wait()

    async with fixture(backend, tmp_path, monkeypatch, priced=True, on_request=wait) as (
        http,
        auth,
        token,
        owner,
        store,
        database,
        calls,
    ):
        state = await authorize(http, token)
        pending = asyncio.create_task(
            http.get(
                "/api/v1/calendar/google/callback",
                params={"state": state, "code": "synthetic-code"},
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 3)
            if change == "logout":
                await auth.logout(await auth.authenticate(token))
            elif change == "owner":
                async with database.sessions.begin() as sql:
                    owner_row = await sql.get_one(AppUserRecord, owner)
                    owner_row.status = "inactive"
            elif change == "disconnect":
                await GoogleTokenStore(database).delete(owner)
            elif change == "reauthorize":
                await authorize(http, token)
            elif change == "rollback":
                original = store.path.read_text()
                value = yaml.safe_load(original)
                value["integrations"]["calendar"]["google"]["enabled"] = False
                store.path.write_text(yaml.safe_dump(value))
                await store.reload()
                store.path.write_text(original)
                await store.reload()
            elif change == "key":
                set_runtime_admin_token("rotated-synthetic-state-key")
            else:
                async with database.sessions.begin() as sql:
                    grant_row = (await sql.scalars(select(CalendarOAuthStateRecord))).one()
                    grant_row.created_at = datetime.now(UTC) - timedelta(minutes=20)
                    grant_row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            release.set()
            response = await pending
            assert response.status_code == 409, response.text
            assert calls == ["POST"] and all(client.is_closed for client in calls.clients)
            async with database.sessions() as sql:
                run = (await sql.scalars(select(TaskRunRecord))).one()
                cost = (await sql.scalars(select(ModelCostRecord))).one()
                assert not list(await sql.scalars(select(CalendarOAuthTokenRecord)))
            assert run.status == "failed" and "google_oauth_token_committed" not in run.contract
            assert cost.state == "unknown" and cost.charged_micros == 2000
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize(
    "kind",
    [
        "redirect",
        "http_error",
        "invalid_json",
        "missing_refresh",
        "invalid_refresh",
        "invalid_shape",
    ],
)
async def test_invalid_provider_response_cannot_install_token_or_replay(
    backend: str,
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, priced=True, response_kind=kind) as (
        http,
        _,
        token,
        _,
        _,
        database,
        calls,
    ):
        params = {"state": await authorize(http, token), "code": "synthetic-code"}
        response = await http.get("/api/v1/calendar/google/callback", params=params)
        assert response.status_code == 502, response.text
        assert (
            await http.get("/api/v1/calendar/google/callback", params=params)
        ).status_code == 400
        assert calls == ["POST"] and all(client.is_closed for client in calls.clients)
        async with database.sessions() as sql:
            run = (await sql.scalars(select(TaskRunRecord))).one()
            cost = (await sql.scalars(select(ModelCostRecord))).one()
            assert not list(await sql.scalars(select(CalendarOAuthTokenRecord)))
        assert run.status == "failed" and cost.state == "unknown" and cost.charged_micros == 2000


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_fee_failure_rolls_back_token_and_completed_state(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(sql: AsyncSession, **kwargs: Any) -> None:
        await sql.flush()
        assert (await sql.scalars(select(CalendarOAuthTokenRecord))).one()
        assert (await sql.scalars(select(CalendarOAuthStateRecord))).one().completed_at is not None
        raise BudgetDenied("unit_cost_reservation_inactive")

    monkeypatch.setattr(operation, "settle_unit_cost", fail)
    async with fixture(backend, tmp_path, monkeypatch, priced=True) as (
        http,
        _,
        token,
        _,
        _,
        database,
        calls,
    ):
        response = await http.get(
            "/api/v1/calendar/google/callback",
            params={"state": await authorize(http, token), "code": "synthetic-code"},
        )
        assert response.status_code == 409, response.text
        assert calls == ["POST"]
        async with database.sessions() as sql:
            assert not list(await sql.scalars(select(CalendarOAuthTokenRecord)))
            state = (await sql.scalars(select(CalendarOAuthStateRecord))).one()
            run = (await sql.scalars(select(TaskRunRecord))).one()
            cost = (await sql.scalars(select(ModelCostRecord))).one()
        assert state.consumed_at is not None and state.completed_at is None
        assert run.status == "failed" and "google_oauth_token_committed" not in run.contract
        assert cost.state == "unknown" and cost.charged_micros == 2000


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_invalid_and_denied_callbacks_never_echo_provider_detail(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch) as (http, _, token, _, _, database, calls):
        invalid = await http.get(
            "/api/v1/calendar/google/callback",
            params={"state": "invalid", "error": "synthetic-private-detail"},
        )
        assert invalid.status_code == 400 and "synthetic-private-detail" not in invalid.text
        state = await authorize(http, token)
        for params in [
            {"state": state},
            {"state": state, "code": "synthetic-code", "error": "denied"},
        ]:
            assert (
                await http.get("/api/v1/calendar/google/callback", params=params)
            ).status_code == 400
        params = {"state": state, "error": "synthetic-private-detail"}
        response = await http.get("/api/v1/calendar/google/callback", params=params)
        assert response.status_code == 200 and response.json() == {
            "status": "denied",
            "detail": "google_authorization_denied",
        }
        assert (
            await http.get("/api/v1/calendar/google/callback", params=params)
        ).status_code == 400
        assert not calls and not calls.clients
        async with database.sessions() as sql:
            assert not list(await sql.scalars(select(TaskRunRecord)))


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_expired_state_is_rejected_before_http_and_latest_state_replaces_old(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch) as (http, _, token, _, _, database, calls):
        old = await authorize(http, token)
        current = await authorize(http, token)
        assert (
            await http.get(
                "/api/v1/calendar/google/callback", params={"state": old, "code": "synthetic-code"}
            )
        ).status_code == 400
        async with database.sessions.begin() as sql:
            row = (await sql.scalars(select(CalendarOAuthStateRecord))).one()
            row.created_at = datetime.now(UTC) - timedelta(minutes=20)
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert (
            await http.get(
                "/api/v1/calendar/google/callback",
                params={"state": current, "code": "synthetic-code"},
            )
        ).status_code == 400
        assert not calls and not calls.clients


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_callback_authority_survives_router_recreation_without_replay(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, priced=True) as (
        http,
        auth,
        token,
        _,
        store,
        database,
        calls,
    ):
        state = await authorize(http, token)
        other = FastAPI()
        other.include_router(
            create_calendar_router(
                _service(database, clock=lambda: datetime.now(UTC)),
                auth,
                google_sync=GoogleCalendarSyncService(database, store),
                google_state_key="synthetic-oauth-state-key",
            )
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=other), base_url="http://test"
        ) as restored:
            params = {"state": state, "code": "synthetic-code"}
            assert (
                await restored.get("/api/v1/calendar/google/callback", params=params)
            ).status_code == 200
            assert (
                await http.get("/api/v1/calendar/google/callback", params=params)
            ).status_code == 400
        assert calls == ["POST"]


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_repeated_cancel_joins_oauth_http_close_and_preserves_consumed_state(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    async with fixture(
        backend, tmp_path, monkeypatch, priced=True, close_entered=entered, close_release=release
    ) as (
        http,
        _,
        token,
        _,
        _,
        database,
        calls,
    ):
        state = await authorize(http, token)
        pending = asyncio.create_task(
            http.get(
                "/api/v1/calendar/google/callback",
                params={"state": state, "code": "synthetic-code"},
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 3)
            pending.cancel()
            await asyncio.sleep(0.01)
            pending.cancel()
            await asyncio.sleep(0.01)
            assert not pending.done() and calls == ["POST"]
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert all(client.is_closed for client in calls.clients)
            async with database.sessions() as sql:
                run = (await sql.scalars(select(TaskRunRecord))).one()
                cost = (await sql.scalars(select(ModelCostRecord))).one()
                state_row = (await sql.scalars(select(CalendarOAuthStateRecord))).one()
                assert not list(await sql.scalars(select(CalendarOAuthTokenRecord)))
            assert (
                run.status == "cancelled"
                and cost.state == "unknown"
                and cost.charged_micros == 2000
            )
            assert state_row.consumed_at is not None and state_row.completed_at is None
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("revoke", [False, True])
async def test_key_revocation_after_token_flush_rolls_back_through_cost_wait(
    backend: str,
    revoke: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait(sql: AsyncSession, **kwargs: Any) -> None:
        await sql.flush()
        assert (await sql.scalars(select(CalendarOAuthTokenRecord))).one()
        entered.set()
        await release.wait()
        await settle_unit_cost(sql, **kwargs)

    monkeypatch.setattr(operation, "settle_unit_cost", wait)
    async with fixture(backend, tmp_path, monkeypatch, priced=True) as (
        http,
        _,
        token,
        _,
        _,
        database,
        calls,
    ):
        params = {"state": await authorize(http, token), "code": "synthetic-code"}
        pending = asyncio.create_task(http.get("/api/v1/calendar/google/callback", params=params))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert all(client.is_closed for client in calls.clients)
            async with database.sessions() as sql:
                assert not list(await sql.scalars(select(CalendarOAuthTokenRecord)))
            if revoke:
                set_runtime_admin_token("revoked-oauth-key")
            release.set()
            response = await pending
            assert response.status_code == (409 if revoke else 200), response.text
            async with database.sessions() as sql:
                saved = list(await sql.scalars(select(CalendarOAuthTokenRecord)))
                row = (await sql.scalars(select(CalendarOAuthStateRecord))).one()
                run = (await sql.scalars(select(TaskRunRecord))).one()
            assert bool(saved) is not revoke and (row.completed_at is not None) is not revoke
            assert run.status == ("failed" if revoke else "succeeded")
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_status_is_owner_scoped_and_disconnect_revokes_pending_authorization(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch) as (
        http,
        _,
        token,
        owner,
        _,
        database,
        calls,
    ):
        headers = {"Authorization": f"Bearer {token}"}
        path = "/api/v1/calendar/google/status"
        assert (await http.get(path)).status_code == 401
        assert (await http.get(path, headers=headers)).json() == {
            "configured": True,
            "connected": False,
        }
        await GoogleTokenStore(database).save(owner, "synthetic-existing-refresh", None)
        state = await authorize(http, token)
        assert (await http.get(path, headers=headers)).json() == {
            "configured": True,
            "connected": True,
        }
        assert (await http.delete("/api/v1/calendar/google/token", headers=headers)).json() == {
            "removed": True
        }
        assert (await http.get(path, headers=headers)).json() == {
            "configured": True,
            "connected": False,
        }
        assert (
            await http.get(
                "/api/v1/calendar/google/callback",
                params={"state": state, "code": "synthetic-code"},
            )
        ).status_code == 400
        assert not calls and not calls.clients


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize(("enabled", "exhausted"), [(False, False), (True, False), (True, True)])
async def test_oauth_exchange_inherits_completed_root_quota_or_disabled_trace(
    backend: str,
    enabled: bool,
    exhausted: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, priced=True, enabled=enabled) as (
        http,
        _,
        token,
        owner,
        _,
        database,
        calls,
    ):
        first = {"state": await authorize(http, token), "code": "synthetic-code"}
        assert (await http.get("/api/v1/calendar/google/callback", params=first)).status_code == 200
        async with database.sessions.begin() as sql:
            root = (await sql.scalars(select(TaskRunRecord))).one()
            root.deadline = datetime.now(UTC) - timedelta(seconds=1)
        deadline = datetime.now(UTC) + timedelta(seconds=30)
        budget = RunBudgetConfig.model_validate(root.budget)
        if exhausted:
            budget = budget.model_copy(update={"max_tool_attempts": 1})
        tool = (
            RunToolBudget(
                database,
                run_id=root.id,
                user_id=owner,
                config=budget,
                maintenance=True,
                deadline=deadline,
            )
            if enabled
            else None
        )
        trace = (
            await capture_disabled_trace(
                database, root.id, owner, maintenance=True, expires_at=deadline
            )
            if not enabled
            else None
        )
        state = await authorize(http, token)
        with tool_budget_scope(tool), run_trace_scope(trace):
            response = await http.get(
                "/api/v1/calendar/google/callback",
                params={"state": state, "code": "synthetic-code"},
            )
        assert response.status_code == (409 if exhausted else 200), response.text
        assert len(calls) == (1 if exhausted else 2)
        assert all(client.is_closed for client in calls.clients)
        async with database.sessions() as sql:
            runs = list(await sql.scalars(select(TaskRunRecord)))
            costs = list(await sql.scalars(select(ModelCostRecord)))
        current_root = next(row for row in runs if row.id == root.id)
        child = next(row for row in runs if row.id != root.id)
        assert child.parent_run_id == root.id and current_root.status == "succeeded"
        assert child.status == ("failed" if exhausted else "succeeded")
        assert resource_usage(current_root).get("tool_attempts", 0) == (
            len(calls) if enabled else 0
        )
        assert resource_usage(child).get("tool_attempts", 0) == 0
        child_cost = next(row for row in costs if row.call_id == child.id)
        assert child_cost.charged_micros == (0 if exhausted else 2000)
        assert child_cost.state == ("estimated" if exhausted else "unknown")


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_production_router_uses_chat_session_and_runtime_key_without_environment_key(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARIA_ADMIN_TOKEN", raising=False)
    async with fixture(backend, tmp_path, monkeypatch, priced=True) as (
        _,
        _,
        token,
        owner,
        config,
        database,
        calls,
    ):
        store = DatabaseConfigStore(database, config.path)
        await store.load()
        app = create_app(
            database,
            config_store=store,
            watch_config=False,
            admin_token="synthetic-runtime-oauth-key",
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            state = await authorize(http, token)
            response = await http.get(
                "/api/v1/calendar/google/callback",
                params={"state": state, "code": "synthetic-code"},
            )
        assert response.status_code == 200, response.text
        async with database.sessions() as sql:
            row = (await sql.scalars(select(CalendarOAuthTokenRecord))).one()
        assert row.user_id == owner and calls == ["POST"]
