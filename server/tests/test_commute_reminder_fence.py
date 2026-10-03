"""Calendar sources and replacement reminders share a real transaction boundary."""

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy import update
from test_commute_query_boundary import NOW, Provider
from test_delivery_sqlite_transactions import explicit_transactions
from test_run_cancel_fence import prepared

from app.calendar.service import CalendarService
from app.calendar.store import CalendarStore
from app.commute.service import CommuteRouteError, CommuteService
from app.db import AppUserRecord
from app.tasks.models import TaskStatus
from app.tasks.store import TaskStore
from scripts.benchmark_storage import FixtureStorage


async def fixture(
    storage: FixtureStorage,
) -> tuple[UUID, CommuteService, CalendarService, TaskStore]:
    owner = uuid4()
    async with storage.database.sessions.begin() as session:
        session.add(AppUserRecord(id=owner, display_name="Synthetic owner", status="active"))
    calendar, tasks = CalendarStore(storage.database), TaskStore(storage.database)
    planner = CommuteService(
        calendar, tasks, Provider(), origin="Synthetic origin", clock=lambda: NOW
    )
    return owner, planner, CalendarService(calendar, tasks, clock=lambda: NOW), tasks


async def outing(calendar: CalendarStore, owner: UUID) -> Any:
    return await calendar.create_event(
        user_id=owner,
        title="Synthetic outing",
        starts_at=NOW + timedelta(hours=2),
        ends_at=NOW + timedelta(hours=3),
        location="Synthetic destination",
        now=NOW,
    )


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", ["cancel", "reschedule"])
async def test_calendar_change_cancels_linked_commute_reminder(
    backend: str,
    change: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, planner, calendar, tasks = await fixture(storage)
        source = await outing(calendar.store, owner)
        plan = await planner.plan_commute(owner, source)
        if change == "cancel":
            await calendar.cancel_event(owner, source.id)
        else:
            await calendar.reschedule_event(
                owner,
                source.id,
                starts_at=NOW + timedelta(hours=2, minutes=30),
                ends_at=NOW + timedelta(hours=3, minutes=30),
            )
        assert plan.reminder_task_id is not None
        task = await tasks.get_task(owner, UUID(plan.reminder_task_id))
        assert task.status == "cancelled" and task.next_fire_at is None
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", ["cancel", "reschedule", "location", "inactive"])
async def test_changed_source_during_route_wait_cannot_create_departure_reminder(
    backend: str,
    change: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    plan_task: asyncio.Task[Any] | None = None
    try:
        owner, planner, calendar, tasks = await fixture(storage)
        source = await outing(calendar.store, owner)

        class Paused(Provider):
            async def route(
                self, origin: str, destination: str, **kwargs: Any
            ) -> dict[str, object]:
                started.set()
                await release.wait()
                return await super().route(origin, destination, **kwargs)

        planner._amap = Paused()
        plan_task = asyncio.create_task(planner.plan_commute(owner, source))
        await asyncio.wait_for(started.wait(), 3)
        if change == "cancel":
            await calendar.cancel_event(owner, source.id)
        elif change == "reschedule":
            await calendar.reschedule_event(
                owner,
                source.id,
                starts_at=NOW + timedelta(hours=2, minutes=30),
                ends_at=NOW + timedelta(hours=3, minutes=30),
            )
        elif change == "location":
            await calendar.store.update_event(owner, source.id, location="Changed destination")
        else:
            async with storage.database.sessions.begin() as session:
                await session.execute(
                    update(AppUserRecord).where(AppUserRecord.id == owner).values(status="inactive")
                )
        release.set()
        with pytest.raises(CommuteRouteError):
            await asyncio.wait_for(plan_task, 3)
        active = await tasks.list_tasks(owner, status=TaskStatus.ACTIVE)
        assert not [task for task in active if task.source_ref == f"commute:{source.id}"]
    finally:
        release.set()
        if plan_task is not None:
            if not plan_task.done():
                plan_task.cancel()
            await asyncio.gather(plan_task, return_exceptions=True)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_replacement_insert_failure_preserves_previous_departure_reminder(
    backend: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, planner, calendar, tasks = await fixture(storage)
        source = await outing(calendar.store, owner)
        original = await planner.plan_commute(owner, source)
        assert original.reminder_task_id is not None

        def fail_insert(*args: Any) -> None:
            compiled = args[4].compiled
            if (
                compiled is not None
                and getattr(compiled.statement, "is_insert", False)
                and compiled.statement.table.name == "task_item"
            ):
                raise RuntimeError("synthetic reminder insert failure")

        sql_event.listen(storage.database.engine.sync_engine, "before_cursor_execute", fail_insert)
        try:
            with pytest.raises(RuntimeError, match="synthetic reminder"):
                await planner.plan_commute(owner, source)
        finally:
            sql_event.remove(
                storage.database.engine.sync_engine, "before_cursor_execute", fail_insert
            )
        previous = await tasks.get_task(owner, UUID(original.reminder_task_id))
        assert previous.status == "active" and previous.next_fire_at == original.leave_by
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_parallel_departure_replacement_leaves_one_active_reminder(
    backend: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, planner, calendar, tasks = await fixture(storage)
        source = await outing(calendar.store, owner)

        async def replace_all() -> None:
            values = await asyncio.gather(
                *[planner.plan_commute(owner, source) for _ in range(8)], return_exceptions=True
            )
            for value in values:
                if isinstance(value, BaseException):
                    raise value
            active = await tasks.list_tasks(owner, status=TaskStatus.ACTIVE)
            assert len([task for task in active if task.source_ref == f"commute:{source.id}"]) == 1

        if backend == "sqlite":
            async with explicit_transactions(storage.database):
                await replace_all()
        else:
            await replace_all()
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_current_calendar_and_commute_reminders_coexist(backend: str, tmp_path: Path) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, planner, calendar, tasks = await fixture(storage)
        source = await calendar.create_event(
            owner,
            title="Synthetic outing",
            starts_at=NOW + timedelta(hours=2),
            ends_at=NOW + timedelta(hours=3),
            location="Synthetic destination",
        )
        await planner.plan_commute(owner, source)
        active = await tasks.list_tasks(owner, status=TaskStatus.ACTIVE)
        assert {task.source_ref for task in active} == {
            f"calendar:{source.id}",
            f"commute:{source.id}",
        }
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("operation", ["cancel", "reschedule"])
async def test_source_change_and_task_cleanup_rollback_together(
    backend: str,
    operation: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, planner, calendar, tasks = await fixture(storage)
        source = await outing(calendar.store, owner)
        original = await planner.plan_commute(owner, source)
        assert original.reminder_task_id is not None

        def fail_cleanup(*args: Any) -> None:
            compiled = args[4].compiled
            if (
                compiled is not None
                and getattr(compiled.statement, "is_update", False)
                and compiled.statement.table.name == "task_item"
            ):
                raise RuntimeError("synthetic cleanup failure")

        sql_event.listen(storage.database.engine.sync_engine, "before_cursor_execute", fail_cleanup)
        try:
            with pytest.raises(RuntimeError, match="synthetic cleanup"):
                if operation == "cancel":
                    await calendar.cancel_event(owner, source.id)
                else:
                    await calendar.reschedule_event(owner, source.id, title="Changed title")
        finally:
            sql_event.remove(
                storage.database.engine.sync_engine, "before_cursor_execute", fail_cleanup
            )
        current = await calendar.store.get_event(owner, source.id)
        assert current.status == "active" and current.title == source.title
        task = await tasks.get_task(owner, UUID(original.reminder_task_id))
        assert task.status == "active" and task.next_fire_at == original.leave_by
    finally:
        await storage.close()
