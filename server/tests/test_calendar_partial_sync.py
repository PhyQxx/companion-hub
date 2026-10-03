"""Failed calendar fetches cannot be interpreted as authoritative remote deletion."""

from pathlib import Path
from typing import Any, cast

import pytest
from test_calendar_caldav import YAML_TEMPLATE as BASE_YAML
from test_calendar_google import YAML_TEMPLATE as GOOGLE_YAML
from test_calendar_mirror_reminder_fence import occurrence, stats
from test_commute_query_boundary import NOW
from test_commute_reminder_fence import fixture
from test_run_cancel_fence import prepared

from app.calendar.caldav import CalDavClient, CalDavError, CalDavSyncService
from app.calendar.google import (
    GoogleCalendarClient,
    GoogleCalendarError,
    GoogleCalendarSyncService,
    GoogleTokenStore,
)
from app.calendar.mirror import CalendarMirrorService, MirrorOccurrence
from app.config import ConfigStore
from app.tasks.models import TaskStatus


class Client:
    def __init__(self, provider: str, *, failed: set[str], populated: bool = False) -> None:
        self.provider, self.failed, self.populated = provider, failed, populated
        self.closed = 0

    async def list_calendars(self) -> list[tuple[str, str]]:
        return [("good", "good"), ("failed", "failed")]

    async def fetch_window(self, href: str, **kwargs: Any) -> list[MirrorOccurrence]:
        return await self.list_events(href, **kwargs)

    async def list_events(self, calendar_id: str, **kwargs: Any) -> list[MirrorOccurrence]:
        if calendar_id in self.failed:
            if self.provider == "caldav":
                raise CalDavError("synthetic_fetch_failed")
            raise GoogleCalendarError("synthetic_fetch_failed")
        if not self.populated:
            return []
        value = occurrence(self.provider)
        value.ref = f"{self.provider}:{calendar_id}"
        return [value]

    async def close(self) -> None:
        self.closed += 1


async def configured(tmp_path: Path, provider: str) -> ConfigStore:
    if provider == "google":
        yaml = GOOGLE_YAML.replace(
            "window_days_forward: 30", "window_days_forward: 30\n      calendar_ids: [good, failed]"
        )
    else:
        yaml = (
            BASE_YAML
            + """
integrations:
  calendar:
    caldav:
      enabled: true
      url: https://fixture.invalid/calendar
      username: fixture
      secret_value: fixture-secret
"""
        )
    path = tmp_path / "partial-sync.yaml"
    path.write_text(yaml)
    config = ConfigStore(path)
    await config.load()
    return config


async def sync_service(database: Any, config: ConfigStore, client: Client, owner: Any) -> Any:
    if client.provider == "google":
        await GoogleTokenStore(database).save(owner, "fixture-refresh", None)
        return GoogleCalendarSyncService(
            database,
            config,
            client_factory=lambda **kwargs: cast(GoogleCalendarClient, client),
            clock=lambda: NOW,
        )
    return CalDavSyncService(
        database,
        config,
        client_factory=lambda **kwargs: cast(CalDavClient, client),
        clock=lambda: NOW,
    )


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
@pytest.mark.parametrize("failed", [{"failed"}, {"good", "failed"}])
async def test_failed_fetch_preserves_existing_mirror_and_departure(
    backend: str,
    provider: str,
    failed: set[str],
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, planner, calendar, tasks = await fixture(storage)
        source = occurrence(provider)
        source.ref = f"{provider}:failed"
        await CalendarMirrorService(storage.database, source=provider).apply(
            owner, f"{provider}:failed", {source.ref: source}, stats=stats(), now=NOW
        )
        (view,) = await calendar.store.list_events(owner)
        await planner.plan_commute(owner, view)
        client = Client(provider, failed=failed)
        service = await sync_service(
            storage.database, await configured(tmp_path, provider), client, owner
        )
        report = await service.sync_once()
        assert len(report.errors) == len(failed)
        assert report.mirrors_cancelled == 0
        active = await calendar.store.list_events(owner)
        assert len(active) == 1 and active[0].id == view.id
        reminders = await tasks.list_tasks(owner, status=TaskStatus.ACTIVE)
        assert [task.source_ref for task in reminders] == [f"commute:{view.id}"]
        assert client.closed == (1 if provider == "caldav" else 2)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
async def test_each_mirror_keeps_its_actual_calendar_identity(
    backend: str,
    provider: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, _, calendar, _ = await fixture(storage)
        client = Client(provider, failed=set(), populated=True)
        service = await sync_service(
            storage.database, await configured(tmp_path, provider), client, owner
        )
        report = await service.sync_once()
        assert report.errors == [] and report.mirrors_created == 2
        records = await CalendarMirrorService(storage.database, source=provider).local_mirrors(
            owner
        )
        assert records[f"{provider}:good"].calendar_id == f"{provider}:good"
        assert records[f"{provider}:failed"].calendar_id == f"{provider}:failed"
        assert len(await calendar.store.list_events(owner)) == 2
    finally:
        await storage.close()


class RetainedClient(Client):
    def __init__(self, provider: str, *, shared_ref: bool = False) -> None:
        super().__init__(provider, failed=set(), populated=True)
        self.first = occurrence(provider)
        self.first.ref = f"{provider}:good"
        self.first.summary = "accepted first calendar"
        self.shared_ref = shared_ref

    async def list_events(self, calendar_id: str, **kwargs: Any) -> list[MirrorOccurrence]:
        if calendar_id == "good":
            return [self.first]
        self.first.summary = "late mutation from retained client"
        values = await super().list_events(calendar_id, **kwargs)
        if self.shared_ref:
            values[0].ref = self.first.ref
        return values

    async def close(self) -> None:
        if self.provider == "google":
            self.first.summary = "late mutation during close"
        await super().close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
async def test_each_fetch_owns_its_projection_before_later_client_waits(
    backend: str, provider: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, _, calendar, _ = await fixture(storage)
        client = RetainedClient(provider)
        service = await sync_service(
            storage.database, await configured(tmp_path, provider), client, owner
        )
        assert not (await service.sync_once()).errors
        records = await CalendarMirrorService(storage.database, source=provider).local_mirrors(
            owner
        )
        assert records[f"{provider}:good"].title == "accepted first calendar"
        assert getattr(client.first, "calendar_id", None) is None
        assert len(await calendar.store.list_events(owner)) == 2
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
async def test_cross_calendar_reference_collision_is_not_a_successful_overwrite(
    backend: str, provider: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, _, calendar, _ = await fixture(storage)
        source = occurrence(provider)
        source.ref = f"{provider}:good"
        await CalendarMirrorService(storage.database, source=provider).apply(
            owner, f"{provider}:good", {source.ref: source}, stats=stats(), now=NOW
        )
        (before,) = await calendar.store.list_events(owner)
        client = RetainedClient(provider, shared_ref=True)
        service = await sync_service(
            storage.database, await configured(tmp_path, provider), client, owner
        )
        report = await service.sync_once()
        assert report.errors == ["calendar_reference_ambiguous"]
        assert report.mirrors_created == report.mirrors_updated == report.mirrors_cancelled == 0
        (after,) = await calendar.store.list_events(owner)
        assert after == before
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
async def test_partial_snapshot_can_update_known_source_and_complete_snapshot_can_remove(
    backend: str, provider: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, planner, calendar, tasks = await fixture(storage)
        source = occurrence(provider)
        source.ref = f"{provider}:good"
        source.summary = "old calendar title"
        await CalendarMirrorService(storage.database, source=provider).apply(
            owner, f"{provider}:good", {source.ref: source}, stats=stats(), now=NOW
        )
        (before,) = await calendar.store.list_events(owner)
        await planner.plan_commute(owner, before)
        client = Client(provider, failed={"failed"}, populated=True)
        config = await configured(tmp_path, provider)
        service = await sync_service(storage.database, config, client, owner)
        report = await service.sync_once()
        assert len(report.errors) == 1 and report.mirrors_updated == 1
        assert report.mirrors_cancelled == 0
        (after,) = await calendar.store.list_events(owner)
        assert after.id == before.id and after.title != before.title
        assert await tasks.list_tasks(owner, status=TaskStatus.ACTIVE) == []
        client.failed.clear()
        client.populated = False
        report = await service.sync_once()
        assert report.errors == [] and report.mirrors_cancelled == 1
        assert await calendar.store.list_events(owner) == []
    finally:
        await storage.close()
