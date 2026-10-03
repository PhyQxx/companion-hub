"""Private meeting actions persist without downgrading their task and run privacy."""

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_commute_query_boundary import NOW
from test_meetings import FakeSummarizer
from test_run_cancel_fence import prepared

from app.calendar.store import CalendarStore
from app.db import AppUserRecord, TaskRunRecord
from app.meetings.models import TranscriptSegment
from app.meetings.service import MeetingService
from app.meetings.store import MeetingStore
from app.schemas import PrivacyLevel
from app.tasks.models import TaskKind, TaskTrigger
from app.tasks.scheduler import TRIGGER_KIND_TIME, TaskScheduler
from app.tasks.store import TaskStore


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("concurrent", [False, True])
async def test_private_action_confirmation_keeps_owned_task_and_delivery_run_private(
    backend: str, concurrent: bool, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner = uuid4()
        async with storage.database.sessions.begin() as session:
            session.add(AppUserRecord(id=owner, display_name="Fixture", status="active"))
        tasks = TaskStore(storage.database)
        meetings = MeetingService(
            MeetingStore(storage.database),
            CalendarStore(storage.database),
            tasks,
            FakeSummarizer(),
            clock=lambda: NOW,
        )
        view = await meetings.prepare(
            owner,
            title="Private synthetic meeting",
            participants=["self"],
            privacy_level=PrivacyLevel.L2,
        )
        await meetings.authorize_transcription(owner, view.id)
        await meetings.append_transcript(
            owner,
            view.id,
            [TranscriptSegment(speaker="self", text="Synthetic consented transcript")],
        )
        await meetings.finish(owner, view.id)
        due = NOW + timedelta(hours=2)
        if concurrent:
            results = await asyncio.gather(
                *[meetings.confirm_action_item(owner, view.id, 0, due_at=due) for _ in range(8)]
            )
        else:
            results = [
                await meetings.confirm_action_item(owner, view.id, 0, due_at=due),
                await meetings.confirm_action_item(owner, view.id, 0, due_at=due),
            ]
        (task,) = await tasks.list_tasks(owner)
        assert task.privacy_level == PrivacyLevel.L2 and task.user_id == owner
        assert all(result.action_items[0].task_id == task.id for result in results)
        (claimed,) = await tasks.claim_due(now=due + timedelta(seconds=1))
        assert claimed.privacy_level == PrivacyLevel.L2 and claimed.id == task.id
        assert claimed.run_id is not None
        dispatched: list[PrivacyLevel] = []

        async def deliver(text: str, *, privacy_level: PrivacyLevel, **kwargs: Any) -> list[str]:
            dispatched.append(privacy_level)
            return ["web_chat"]

        scheduler = TaskScheduler(
            tasks, clock=lambda: due + timedelta(seconds=1), deliverer=deliver
        )
        await scheduler._fire(claimed, trigger_kind=TRIGGER_KIND_TIME)
        assert dispatched == [PrivacyLevel.L2]
        async with storage.database.sessions() as session:
            run = await session.scalar(
                select(TaskRunRecord).where(TaskRunRecord.id == claimed.run_id)
            )
            assert run is not None and run.privacy_level == "L2" and run.user_id == owner
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("raw", [PrivacyLevel.L3, "L3"])
async def test_ephemeral_task_is_rejected_before_persistent_write(
    backend: str, raw: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner = uuid4()
        async with storage.database.sessions.begin() as session:
            session.add(AppUserRecord(id=owner, display_name="Fixture", status="active"))
        tasks = TaskStore(storage.database)
        with pytest.raises(ValueError, match="task_privacy_level_must_be_persistent"):
            await tasks.create(
                user_id=owner,
                kind=TaskKind.TASK,
                title="Ephemeral fixture",
                trigger=TaskTrigger(type="time", at=NOW + timedelta(hours=1)),
                privacy_level=cast(PrivacyLevel, raw),
                now=NOW,
            )
        assert await tasks.list_tasks(owner) == []
    finally:
        await storage.close()
