"""External calendar mirror changes invalidate the same owned reminder lineage."""

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_commute_query_boundary import NOW
from test_commute_reminder_fence import fixture
from test_delivery_sqlite_transactions import explicit_transactions
from test_run_cancel_fence import prepared

from app.calendar.mirror import CalendarMirrorService, MirrorOccurrence
from app.db import CalendarEventRecord
from app.tasks.models import TaskStatus


def stats() -> SimpleNamespace:
    return SimpleNamespace(mirrors_created=0, mirrors_updated=0, mirrors_cancelled=0)


def occurrence(source: str) -> MirrorOccurrence:
    return MirrorOccurrence(
        ref=f"{source}:synthetic",
        summary="Synthetic external outing",
        starts_at=NOW + timedelta(hours=2),
        ends_at=NOW + timedelta(hours=3),
        location="Synthetic destination",
        etag="v1",
    )


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
@pytest.mark.parametrize("change", ["cancel", "missing", "update"])
async def test_mirror_change_cancels_previously_created_departure_reminder(
    backend: str,
    provider: str,
    change: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, planner, calendar, tasks = await fixture(storage)
        mirror = CalendarMirrorService(storage.database, source=provider)
        source = occurrence(provider)
        await mirror.apply(owner, "external", {source.ref: source}, stats=stats(), now=NOW)
        (view,) = await calendar.store.list_events(owner)
        plan = await planner.plan_commute(owner, view)
        assert plan.reminder_task_id is not None
        source.etag = "v2"
        if change == "cancel":
            source.cancelled = True
        elif change == "update":
            source.location = "Changed external destination"
        await mirror.apply(
            owner,
            "external",
            {} if change == "missing" else {source.ref: source},
            stats=stats(),
            now=NOW + timedelta(seconds=1),
        )
        active = await tasks.list_tasks(owner, status=TaskStatus.ACTIVE)
        assert not [task for task in active if task.source_ref == f"commute:{view.id}"]
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
async def test_parallel_mirror_acceptance_creates_one_source_per_reference(
    backend: str,
    provider: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, _, _, _ = await fixture(storage)
        source = occurrence(provider)
        mirror = CalendarMirrorService(storage.database, source=provider)
        reports = [stats() for _ in range(8)]

        async def apply_all() -> None:
            results = await asyncio.gather(
                *[
                    mirror.apply(owner, "external", {source.ref: source}, stats=report, now=NOW)
                    for report in reports
                ],
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            async with storage.database.sessions() as session:
                rows = list(
                    await session.scalars(
                        select(CalendarEventRecord).where(CalendarEventRecord.user_id == owner)
                    )
                )
            assert len(rows) == 1
            assert sum(report.mirrors_created for report in reports) == 1

        if backend == "sqlite":
            async with explicit_transactions(storage.database):
                await apply_all()
        else:
            await apply_all()
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", ["collection", "occurrence"])
async def test_mirror_acceptance_owns_input_before_first_query_wait(
    backend: str,
    change: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = await prepared(backend, tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    task: asyncio.Task[Any] | None = None
    try:
        owner, _, calendar, _ = await fixture(storage)
        source = occurrence("google")
        inputs = {source.ref: source}
        original = AsyncSession.scalars

        async def paused(session: AsyncSession, statement: Any, *args: Any, **kwargs: Any) -> Any:
            if (
                getattr(statement, "is_select", False)
                and statement.column_descriptions[0].get("entity") is CalendarEventRecord
            ):
                entered.set()
                await release.wait()
            return await original(session, statement, *args, **kwargs)

        monkeypatch.setattr(AsyncSession, "scalars", paused)
        task = asyncio.create_task(
            CalendarMirrorService(storage.database, source="google").apply(
                owner, "external", inputs, stats=stats(), now=NOW
            )
        )
        await asyncio.wait_for(entered.wait(), 3)
        if change == "collection":
            inputs.clear()
        else:
            source.summary = "Changed caller summary"
        release.set()
        await asyncio.wait_for(task, 3)
        views = await calendar.store.list_events(owner)
        assert len(views) == 1 and views[0].title == "Synthetic external outing"
    finally:
        release.set()
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
async def test_mirror_cleanup_failure_rolls_back_source_change(
    backend: str,
    provider: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, planner, calendar, tasks = await fixture(storage)
        mirror = CalendarMirrorService(storage.database, source=provider)
        source = occurrence(provider)
        await mirror.apply(owner, "external", {source.ref: source}, stats=stats(), now=NOW)
        (view,) = await calendar.store.list_events(owner)
        await planner.plan_commute(owner, view)

        def fail_cleanup(*args: Any) -> None:
            compiled = args[4].compiled
            if (
                compiled is not None
                and getattr(compiled.statement, "is_update", False)
                and compiled.statement.table.name == "task_item"
            ):
                raise RuntimeError("synthetic mirror cleanup failure")

        sql_event.listen(storage.database.engine.sync_engine, "before_cursor_execute", fail_cleanup)
        try:
            with pytest.raises(RuntimeError, match="synthetic mirror cleanup"):
                await mirror.apply(
                    owner, "external", {}, stats=stats(), now=NOW + timedelta(seconds=1)
                )
        finally:
            sql_event.remove(
                storage.database.engine.sync_engine, "before_cursor_execute", fail_cleanup
            )
        current = await calendar.store.get_event(owner, view.id)
        assert current.status == "active"
        active = await tasks.list_tasks(owner, status=TaskStatus.ACTIVE)
        assert len([task for task in active if task.source_ref == f"commute:{view.id}"]) == 1
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
@pytest.mark.parametrize("etag", ["", "v1"])
async def test_unchanged_mirror_keeps_valid_departure_and_reports_no_update(
    backend: str,
    provider: str,
    etag: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, planner, calendar, tasks = await fixture(storage)
        source = occurrence(provider)
        source.etag = etag
        mirror = CalendarMirrorService(storage.database, source=provider)
        await mirror.apply(owner, "external", {source.ref: source}, stats=stats(), now=NOW)
        (view,) = await calendar.store.list_events(owner)
        await planner.plan_commute(owner, view)
        report = stats()
        await mirror.apply(
            owner, "external", {source.ref: source}, stats=report, now=NOW + timedelta(seconds=1)
        )
        assert report.mirrors_updated == report.mirrors_created == report.mirrors_cancelled == 0
        active = await tasks.list_tasks(owner, status=TaskStatus.ACTIVE)
        assert len([task for task in active if task.source_ref == f"commute:{view.id}"]) == 1
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("provider", ["caldav", "google"])
async def test_remote_reappearance_reactivates_a_cancelled_mirror_with_same_etag(
    backend: str,
    provider: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, _, calendar, _ = await fixture(storage)
        source = occurrence(provider)
        mirror = CalendarMirrorService(storage.database, source=provider)
        await mirror.apply(owner, "external", {source.ref: source}, stats=stats(), now=NOW)
        await mirror.apply(owner, "external", {}, stats=stats(), now=NOW + timedelta(seconds=1))
        report = stats()
        await mirror.apply(
            owner, "external", {source.ref: source}, stats=report, now=NOW + timedelta(seconds=2)
        )
        active = await calendar.store.list_events(owner)
        assert len(active) == 1 and report.mirrors_updated == 1
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_legacy_duplicate_mirrors_keep_latest_identity_and_retire_extra_tasks(
    backend: str,
    tmp_path: Path,
) -> None:
    from app.calendar.mirror import _mirror_record

    storage = await prepared(backend, tmp_path)
    try:
        owner, planner, calendar, tasks = await fixture(storage)
        source = occurrence("google")
        mirror = CalendarMirrorService(storage.database, source="google")
        await mirror.apply(owner, "external", {source.ref: source}, stats=stats(), now=NOW)
        latest = _mirror_record(owner, "google", "external", source, NOW + timedelta(seconds=1))
        async with storage.database.sessions.begin() as session:
            session.add(latest)
        views = await calendar.store.list_events(owner)
        assert len(views) == 2
        for view in views:
            await planner.plan_commute(owner, view)
        report = stats()
        await mirror.apply(
            owner, "external", {source.ref: source}, stats=report, now=NOW + timedelta(seconds=2)
        )
        active_sources = await calendar.store.list_events(owner)
        assert len(active_sources) == 1 and active_sources[0].id == latest.id
        active = await tasks.list_tasks(owner, status=TaskStatus.ACTIVE)
        assert [task.source_ref for task in active] == [f"commute:{latest.id}"]
        assert report.mirrors_cancelled == 1
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_mirror_inventory_does_not_omit_rows_after_2000(backend: str, tmp_path: Path) -> None:
    from app.calendar.mirror import _mirror_record

    storage = await prepared(backend, tmp_path)
    try:
        owner, _, _, _ = await fixture(storage)
        rows = []
        for index in range(2001):
            source = occurrence("google")
            source.ref = f"google:synthetic:{index}"
            rows.append(_mirror_record(owner, "google", "external", source, NOW))
        async with storage.database.sessions.begin() as session:
            session.add_all(rows)
        records = await CalendarMirrorService(storage.database, source="google").local_mirrors(
            owner
        )
        assert len(records) == 2001 and "google:synthetic:2000" in records
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", ["key", "length"])
async def test_invalid_mirror_reference_does_not_create_unaddressable_source(
    backend: str,
    change: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, _, calendar, _ = await fixture(storage)
        source = occurrence("google")
        if change == "length":
            source.ref = "g" * 161
        inputs = {"wrong" if change == "key" else source.ref: source}
        with pytest.raises(ValueError, match="reference"):
            await CalendarMirrorService(storage.database, source="google").apply(
                owner, "external", inputs, stats=stats(), now=NOW
            )
        assert await calendar.store.list_events(owner, include_cancelled=True) == []
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_inactive_owner_does_not_accept_remote_mirror_data(
    backend: str, tmp_path: Path
) -> None:
    from sqlalchemy import update

    from app.db import AppUserRecord

    storage = await prepared(backend, tmp_path)
    try:
        owner, _, calendar, _ = await fixture(storage)
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(AppUserRecord).where(AppUserRecord.id == owner).values(status="inactive")
            )
        source = occurrence("google")
        with pytest.raises(PermissionError, match="inactive"):
            await CalendarMirrorService(storage.database, source="google").apply(
                owner, "external", {source.ref: source}, stats=stats(), now=NOW
            )
        assert await calendar.store.list_events(owner, include_cancelled=True) == []
    finally:
        await storage.close()
