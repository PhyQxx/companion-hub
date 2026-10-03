"""Calendar detail looks up its own active reminder beyond unrelated task pages."""

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from test_commute_query_boundary import NOW
from test_commute_reminder_fence import fixture
from test_run_cancel_fence import prepared

from app.db import TaskItemRecord


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_calendar_reminder_does_not_disappear_behind_500_newer_tasks(
    backend: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, _, calendar, _ = await fixture(storage)
        source = await calendar.create_event(
            owner,
            title="Fixture calendar",
            starts_at=NOW + timedelta(hours=2),
            ends_at=NOW + timedelta(hours=3),
        )
        assert source.reminder_task_id is not None
        async with storage.database.sessions.begin() as session:
            session.add_all(
                [
                    TaskItemRecord(
                        id=uuid4(),
                        user_id=owner,
                        kind="reminder",
                        title="Newer unrelated fixture",
                        status="active",
                        trigger_type="time",
                        trigger_config={
                            "type": "time",
                            "at": (NOW + timedelta(days=1)).isoformat(),
                        },
                        source="fixture",
                        source_ref=f"fixture:{index}",
                        next_fire_at=NOW + timedelta(days=1),
                        fire_count=0,
                        privacy_level="L1",
                        created_at=NOW + timedelta(minutes=1),
                        updated_at=NOW + timedelta(minutes=1),
                    )
                    for index in range(600)
                ]
            )
        view = await calendar._view_with_reminder(owner, source.id)
        assert view.reminder_task_id == source.reminder_task_id
    finally:
        await storage.close()
