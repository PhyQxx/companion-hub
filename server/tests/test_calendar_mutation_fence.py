"""Waiting calendar writes must use current owner/status without clearing valid tasks."""

from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from test_commute_query_boundary import NOW
from test_commute_reminder_fence import fixture, outing
from test_job_lifecycle_fence import during_commit
from test_run_cancel_fence import prepared

from app.db import AppUserRecord, CalendarEventRecord


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("operation", ["update", "cancel"])
@pytest.mark.parametrize("change", ["owner", "status"])
async def test_waiting_calendar_mutation_does_not_overwrite_changed_authority(
    backend: str,
    operation: str,
    change: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, planner, calendar, tasks = await fixture(storage)
        other = uuid4()
        async with storage.database.sessions.begin() as session:
            session.add(AppUserRecord(id=other, display_name="Synthetic other", status="active"))
        source = await outing(calendar.store, owner)
        original = await planner.plan_commute(owner, source)
        assert original.reminder_task_id is not None

        async def rejected() -> None:
            with pytest.raises(LookupError if change == "owner" else ValueError):
                if operation == "update":
                    await calendar.store.update_event(owner, source.id, title="Late title")
                else:
                    await calendar.store.cancel_event(owner, source.id)

        await during_commit(
            storage.database,
            lambda session: session.execute(
                update(CalendarEventRecord)
                .where(CalendarEventRecord.id == source.id)
                .values(
                    **({"user_id": other} if change == "owner" else {"status": "cancelled"}),
                )
            ),
            rejected,
        )
        async with storage.database.sessions() as session:
            current = await session.get_one(CalendarEventRecord, source.id)
            assert current.title == source.title
            assert current.user_id == (other if change == "owner" else owner)
            assert current.status == ("active" if change == "owner" else "cancelled")
        task = await tasks.get_task(owner, UUID(original.reminder_task_id))
        assert task.status == "active" and task.next_fire_at == original.leave_by
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_partial_reschedule_compares_sqlite_timestamps_in_utc(
    backend: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, _, calendar, _ = await fixture(storage)
        source = await outing(calendar.store, owner)
        updated = await calendar.store.update_event(
            owner,
            source.id,
            starts_at=NOW + timedelta(hours=2, minutes=15),
        )
        assert updated.starts_at == NOW + timedelta(hours=2, minutes=15)
        assert updated.ends_at == source.ends_at
    finally:
        await storage.close()
