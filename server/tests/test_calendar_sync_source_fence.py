"""Calendar fetches from withdrawn connections cannot commit stale mirrors."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session
from test_calendar import _service
from test_calendar_mirror_reminder_fence import occurrence, stats
from test_calendar_partial_sync import Client, configured, sync_service
from test_resource_budget import seed
from test_run_cancel_fence import prepared

from app.api.calendar import create_calendar_router
from app.auth import AuthService
from app.calendar.caldav import CalDavClient
from app.calendar.google import GoogleCalendarClient, GoogleTokenStore
from app.calendar.mirror import CalendarMirrorService, MirrorOccurrence
from app.calendar.sync_sources import CalendarSyncSource
from app.db import AppUserRecord, AuthSessionRecord, CalendarEventRecord
from app.ids import uuid7


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
@pytest.mark.parametrize(
    "change",
    ["disable", "rollback", "secret", "owner", "logout", "expire", "env_rotate", "env_remove"],
)
async def test_sync_withdrawn_while_fetching_cannot_commit_mirrors(
    backend: str, provider: str, change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = await prepared(backend, tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    class Blocking(Client):
        async def list_events(self, calendar_id: str, **kwargs: object) -> list[MirrorOccurrence]:
            entered.set()
            await release.wait()
            return [occurrence(provider)]

    client = Blocking(provider, failed=set())
    try:
        owner, _, _ = await seed(storage.database)
        auth = AuthService(storage.database)
        actor = await auth.setup(display_name="Calendar fixture", password="synthetic password")
        assert actor.principal.user_id == owner
        config = await configured(tmp_path, provider)
        if change.startswith("env_"):
            monkeypatch.setenv("CALENDAR_FIXTURE_SECRET", "accepted-fixture-secret")
            original_secret = "fixture-secret" if provider == "caldav" else "google-secret"
            config.path.write_text(
                config.path.read_text().replace(
                    f"secret_value: {original_secret}", "secret_ref: env:CALENDAR_FIXTURE_SECRET"
                )
            )
            await config.reload()
        service = await sync_service(storage.database, config, client, owner)
        pending = asyncio.create_task(
            service.sync_once(user_id=owner, actor_id=actor.principal.session_id)
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            original = config.path.read_text()
            if change in {"disable", "rollback", "secret"}:
                config.path.write_text(
                    original.replace("enabled: true", "enabled: false")
                    if change != "secret"
                    else original.replace("secret_value:", "secret_value: changed-")
                )
                await config.reload()
                if change == "rollback":
                    config.path.write_text(original)
                    await config.reload()
            elif change == "env_rotate":
                monkeypatch.setenv("CALENDAR_FIXTURE_SECRET", "changed-fixture-secret")
            elif change == "env_remove":
                monkeypatch.delenv("CALENDAR_FIXTURE_SECRET")
            elif change == "logout":
                await auth.logout(actor.principal)
            else:
                async with storage.database.sessions.begin() as sql:
                    if change == "owner":
                        await sql.execute(
                            update(AppUserRecord)
                            .where(AppUserRecord.id == owner)
                            .values(status="inactive")
                        )
                    else:
                        await sql.execute(
                            update(AuthSessionRecord)
                            .where(AuthSessionRecord.id == actor.principal.session_id)
                            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                        )
            release.set()
            with pytest.raises(
                PermissionError,
                match=r"calendar_sync_(source_changed|owner_inactive|actor_invalid)",
            ):
                await pending
            async with storage.database.sessions() as sql:
                assert not list(await sql.scalars(select(CalendarEventRecord)))
            assert client.closed >= 1
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
async def test_inflight_withdrawal_waits_for_slow_sdk_finally(
    backend: str, provider: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    entered, cleaned = asyncio.Event(), asyncio.Event()

    class Blocking(Client):
        async def list_events(self, calendar_id: str, **kwargs: object) -> list[MirrorOccurrence]:
            entered.set()
            try:
                await asyncio.Event().wait()
                return []
            finally:
                await asyncio.sleep(0.35)
                cleaned.set()

    client = Blocking(provider, failed=set())
    try:
        owner, _, _ = await seed(storage.database)
        config = await configured(tmp_path, provider)
        service = await sync_service(storage.database, config, client, owner)
        pending = asyncio.create_task(service.sync_once(user_id=owner))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            config.path.write_text(
                config.path.read_text().replace("enabled: true", "enabled: false")
            )
            await config.reload()
            with pytest.raises(PermissionError, match="calendar_sync_source_changed"):
                await asyncio.wait_for(pending, 3)
            assert cleaned.is_set() and client.closed == 1
            async with storage.database.sessions() as sql:
                assert not list(await sql.scalars(select(CalendarEventRecord)))
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", ["delete", "rotate", "regrant"])
async def test_google_withdrawn_refresh_grant_cannot_commit(
    backend: str, change: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    class Blocking(Client):
        async def list_events(self, calendar_id: str, **kwargs: object) -> list[MirrorOccurrence]:
            entered.set()
            await release.wait()
            return [occurrence("google")]

    client = Blocking("google", failed=set())
    try:
        owner, _, _ = await seed(storage.database)
        service = await sync_service(
            storage.database, await configured(tmp_path, "google"), client, owner
        )
        pending = asyncio.create_task(service.sync_once(user_id=owner))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            tokens = GoogleTokenStore(storage.database)
            if change == "delete":
                await tokens.delete(owner)
            else:
                await tokens.save(
                    owner, "rotated" if change == "rotate" else "fixture-refresh", None
                )
            release.set()
            with pytest.raises(PermissionError, match="calendar_sync_source_changed"):
                await pending
            async with storage.database.sessions() as sql:
                assert not list(await sql.scalars(select(CalendarEventRecord)))
            assert client.closed == 1
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
async def test_sync_repeated_cancellation_joins_client_close(
    backend: str, provider: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    entered, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Blocking(Client):
        async def list_events(self, calendar_id: str, **kwargs: object) -> list[MirrorOccurrence]:
            entered.set()
            await asyncio.Event().wait()
            return []

        async def close(self) -> None:
            closing.set()
            await release.wait()
            await super().close()

    client = Blocking(provider, failed=set())
    try:
        owner, _, _ = await seed(storage.database)
        service = await sync_service(
            storage.database, await configured(tmp_path, provider), client, owner
        )
        pending = asyncio.create_task(service.sync_once(user_id=owner))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            pending.cancel()
            await asyncio.wait_for(closing.wait(), 2)
            pending.cancel()
            await asyncio.sleep(0.35)
            assert not pending.done() and client.closed == 0
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert client.closed == 1
            async with storage.database.sessions() as sql:
                assert not list(await sql.scalars(select(CalendarEventRecord)))
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
async def test_source_withdrawal_after_flush_rolls_back_mirror_and_counters(
    backend: str, provider: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    withdrawn = False

    def check() -> None:
        if withdrawn:
            raise PermissionError("calendar_sync_source_changed")

    def withdraw(session: Session, context: object) -> None:
        nonlocal withdrawn
        withdrawn = True

    try:
        owner, _, _ = await seed(storage.database)
        source = CalendarSyncSource(storage.database, owner=owner, settings_guard=check)
        await source.check()
        report = stats()
        value = occurrence(provider)
        event.listen(Session, "after_flush_postexec", withdraw)
        try:
            with pytest.raises(PermissionError, match="calendar_sync_source_changed"):
                await CalendarMirrorService(storage.database, source=provider).apply(
                    owner, "fixture", {value.ref: value}, stats=report, source=source
                )
        finally:
            event.remove(Session, "after_flush_postexec", withdraw)
        assert report.mirrors_created == report.mirrors_cancelled == report.mirrors_updated == 0
        async with storage.database.sessions() as sql:
            assert not list(await sql.scalars(select(CalendarEventRecord)))
        withdrawn = False
        await CalendarMirrorService(storage.database, source=provider).apply(
            owner, "fixture", {value.ref: value}, stats=report, source=source
        )
        assert report.mirrors_created == 1
    finally:
        await storage.close()


@pytest.mark.parametrize("provider", ["caldav", "google"])
async def test_native_requests_stop_before_next_http_after_withdrawal(provider: str) -> None:
    calls: list[str] = []
    withdrawn = False

    async def guard() -> None:
        if withdrawn:
            raise PermissionError("calendar_sync_source_changed")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal withdrawn
        calls.append(request.method)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "fixture", "expires_in": 3600})
        withdrawn = True
        return httpx.Response(200, json={"items": [], "nextPageToken": "next"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        if provider == "google":
            google = GoogleCalendarClient(
                client_id="fixture", client_secret="fixture", refresh_token="fixture", client=http
            )
            google.set_source_guard(guard)
            with pytest.raises(PermissionError, match="calendar_sync_source_changed"):
                await google.list_events(
                    "fixture", start=datetime.now(UTC), end=datetime.now(UTC) + timedelta(days=1)
                )
            assert calls == ["POST", "GET"]
        else:
            caldav = CalDavClient(
                base_url="https://fixture.invalid",
                username="fixture",
                secret="fixture",
                client=http,
            )
            caldav.set_source_guard(guard)
            withdrawn = True
            with pytest.raises(PermissionError, match="calendar_sync_source_changed"):
                await caldav.fetch_window(
                    "/fixture", start=datetime.now(UTC), end=datetime.now(UTC) + timedelta(days=1)
                )
            assert calls == []


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
@pytest.mark.parametrize("logout", [False, True])
async def test_sync_api_uses_authenticated_owner_and_revokes_inflight_session(
    backend: str, provider: str, logout: bool, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    class Blocking(Client):
        async def list_events(self, calendar_id: str, **kwargs: object) -> list[MirrorOccurrence]:
            entered.set()
            await release.wait()
            value = occurrence(provider)
            value.ref = f"{provider}:{calendar_id}"
            return [value]

    client = Blocking(provider, failed=set())
    try:
        auth = AuthService(storage.database)
        actor = await auth.setup(display_name="Calendar fixture", password="synthetic password")
        async with storage.database.sessions.begin() as sql:
            sql.add(
                AppUserRecord(
                    id=uuid7(),
                    display_name="Other owner",
                    status="active",
                    created_at=datetime.now(UTC) - timedelta(days=1),
                )
            )
        service = await sync_service(
            storage.database, await configured(tmp_path, provider), client, actor.principal.user_id
        )
        app = FastAPI()
        app.include_router(
            create_calendar_router(
                _service(storage.database, clock=lambda: datetime.now(UTC)),
                auth,
                **{f"{provider}_sync": service},
            )
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            pending = asyncio.create_task(
                http.post(
                    f"/api/v1/calendar/{provider}/sync",
                    headers={"Authorization": f"Bearer {actor.access_token}"},
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), 2)
                if logout:
                    await auth.logout(actor.principal)
                release.set()
                response = await pending
                assert response.status_code == (409 if logout else 200), response.text
                async with storage.database.sessions() as sql:
                    records = list(await sql.scalars(select(CalendarEventRecord)))
                if logout:
                    assert not records
                    assert response.json()["detail"] == "calendar_sync_source_changed"
                else:
                    assert len(records) == 2
                    assert {row.user_id for row in records} == {actor.principal.user_id}
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    finally:
        await storage.close()
