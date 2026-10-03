"""Source cleanup chunks large reference sets and preserves foreign/terminal tasks."""

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_commute_query_boundary import NOW
from test_run_cancel_fence import prepared

from app.db import AppUserRecord, TaskItemRecord
from app.tasks.store import TaskStore


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_source_cleanup_crosses_chunk_boundary_with_owned_dependents_only(
    backend: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, other = uuid4(), uuid4()
        async with storage.database.sessions.begin() as session:
            session.add_all(
                [
                    AppUserRecord(id=value, display_name="Fixture", status="active")
                    for value in (owner, other)
                ]
            )
        refs = [f"calendar:{uuid4()}" for _ in range(600)]
        rows = []
        for index, ref in enumerate(refs):
            rows.append(
                TaskItemRecord(
                    id=uuid4(),
                    user_id=owner,
                    kind="reminder",
                    title="Synthetic task",
                    status="active",
                    trigger_type="time",
                    trigger_config={"type": "time", "at": (NOW + timedelta(hours=1)).isoformat()},
                    source="fixture",
                    source_ref=ref if index % 2 == 0 else ref.replace("calendar:", "commute:"),
                    next_fire_at=NOW + timedelta(hours=1),
                    fire_count=0,
                    privacy_level="L1",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        foreign, terminal = uuid4(), uuid4()
        for identifier, user, status in ((foreign, other, "active"), (terminal, owner, "done")):
            rows.append(
                TaskItemRecord(
                    id=identifier,
                    user_id=user,
                    kind="reminder",
                    title="Synthetic preserved task",
                    status=status,
                    trigger_type="time",
                    trigger_config={"type": "time", "at": (NOW + timedelta(hours=1)).isoformat()},
                    source="fixture",
                    source_ref=refs[0],
                    next_fire_at=NOW + timedelta(hours=1) if status == "active" else None,
                    fire_count=0,
                    privacy_level="L1",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        async with storage.database.sessions.begin() as session:
            session.add_all(rows)
        async with storage.database.sessions.begin() as session:
            count = await TaskStore(storage.database).cancel_tasks_by_source_refs_in_session(
                session,
                owner,
                refs + refs[:5],
                now=NOW + timedelta(seconds=1),
            )
        assert count == 600
        async with storage.database.sessions() as session:
            changed = list(
                await session.scalars(
                    select(TaskItemRecord).where(
                        TaskItemRecord.user_id == owner, TaskItemRecord.id != terminal
                    )
                )
            )
            assert len(changed) == 600
            assert all(row.status == "cancelled" and row.next_fire_at is None for row in changed)
            assert (await session.get_one(TaskItemRecord, foreign)).status == "active"
            preserved = await session.get_one(TaskItemRecord, terminal)
            assert preserved.status == "done" and preserved.cancelled_at is None
    finally:
        await storage.close()
