"""Calendar coordination owns projections and refuses foreign derived evidence."""

import asyncio
from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from test_commute_query_boundary import NOW, Calendar

from app.calendar.models import CalendarEventView, CalendarParticipant, CalendarPreview
from app.calendar.service import CalendarService
from app.calendar.store import CalendarStore
from app.schemas import PrivacyLevel
from app.tasks.models import TaskKind, TaskStatus, TaskTrigger, TaskView
from app.tasks.store import TaskStore


class Repository(Calendar):
    def __init__(self) -> None:
        super().__init__()
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.wait = False
        self.writes: list[dict[str, Any]] = []

    async def overlapping(self, user_id: object, **kwargs: Any) -> list[CalendarEventView]:
        self.entered.set()
        if self.wait:
            await self.release.wait()
        return self.events

    async def create_event(self, **kwargs: Any) -> CalendarEventView:
        self.entered.set()
        if self.wait:
            await self.release.wait()
        self.writes.append(kwargs)
        return self.event

    async def update_event(
        self, user_id: object, event_id: object, **kwargs: Any
    ) -> CalendarEventView:
        self.writes.append(kwargs)
        return self.event

    async def get_event(self, user_id: object, event_id: object) -> CalendarEventView:
        return self.event

    async def cancel_event(self, user_id: object, event_id: object) -> CalendarEventView:
        return self.event.model_copy(update={"status": "cancelled"})


class Tasks:
    def __init__(self) -> None:
        self.calls: list[CalendarEventView] = []
        self.views: list[TaskView] = []

    async def replace_calendar_reminder(
        self, user_id: object, event: CalendarEventView, **kwargs: Any
    ) -> TaskView:
        self.calls.append(event)
        return task(event)

    async def list_tasks(self, user_id: object, **kwargs: Any) -> list[TaskView]:
        return self.views

    async def find_active_by_source_ref(self, user_id: object, source_ref: str) -> TaskView | None:
        return self.views[0] if self.views else None


def task(event: CalendarEventView) -> TaskView:
    return TaskView(
        id=uuid4(),
        user_id=event.user_id,
        kind=TaskKind.REMINDER,
        title="Fixture reminder",
        status=TaskStatus.ACTIVE,
        trigger=TaskTrigger(type="time", at=event.starts_at),
        privacy_level=PrivacyLevel.L1,
        source="calendar",
        source_ref=f"calendar:{event.id}",
    )


def service(repository: Repository, tasks: Tasks) -> CalendarService:
    return CalendarService(
        cast(CalendarStore, repository), cast(TaskStore, tasks), clock=lambda: NOW
    )


@pytest.mark.parametrize("operation", ["preview", "create"])
async def test_participants_are_owned_before_repository_wait(operation: str) -> None:
    repository, tasks = Repository(), Tasks()
    repository.wait = True
    participants = [CalendarParticipant(name="accepted participant")]
    method = (
        service(repository, tasks).preview
        if operation == "preview"
        else service(repository, tasks).create_event
    )
    pending = asyncio.create_task(
        method(
            repository.owner,
            title="Fixture event",
            starts_at=repository.event.starts_at,
            ends_at=repository.event.ends_at,
            participants=participants,
        )
    )
    try:
        await asyncio.wait_for(repository.entered.wait(), 3)
        participants.clear()
        repository.release.set()
        result = await asyncio.wait_for(pending, 3)
        if operation == "preview":
            assert isinstance(result, CalendarPreview)
            assert [value.name for value in result.participants] == ["accepted participant"]
        else:
            assert [value.name for value in repository.writes[0]["participants"]] == [
                "accepted participant"
            ]
    finally:
        repository.release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_preview_conflicts_are_owned_and_filtered_to_owned_active_overlap() -> None:
    repository, tasks = Repository(), Tasks()
    accepted = repository.event.model_copy(
        update={"participants": [CalendarParticipant(name="original")]}
    )
    repository.events = [
        accepted,
        accepted.model_copy(update={"id": uuid4(), "user_id": uuid4()}),
        accepted.model_copy(update={"id": uuid4(), "status": "cancelled"}),
        accepted.model_copy(
            update={
                "id": uuid4(),
                "starts_at": accepted.ends_at,
                "ends_at": accepted.ends_at + timedelta(hours=1),
            }
        ),
    ]
    preview = await service(repository, tasks).preview(
        repository.owner,
        title="Fixture preview",
        starts_at=accepted.starts_at,
        ends_at=accepted.ends_at,
    )
    accepted.participants.clear()
    assert [value.id for value in preview.conflicts] == [accepted.id]
    assert [value.name for value in preview.conflicts[0].participants] == ["original"]
    assert not repository.writes and not tasks.calls


@pytest.mark.parametrize("lead", [-1, 1441])
async def test_invalid_reschedule_lead_is_rejected_before_calendar_write(lead: int) -> None:
    repository, tasks = Repository(), Tasks()
    with pytest.raises(ValueError, match="提前量"):
        await service(repository, tasks).reschedule_event(
            repository.owner, repository.event.id, title="New title", reminder_lead_minutes=lead
        )
    assert repository.writes == [] and tasks.calls == []


@pytest.mark.parametrize("changed", ["user_id", "status"])
async def test_invalid_created_projection_does_not_authorize_reminder(changed: str) -> None:
    repository, tasks = Repository(), Tasks()
    repository.event = repository.event.model_copy(
        update={changed: uuid4() if changed == "user_id" else "cancelled"}
    )
    with pytest.raises(LookupError):
        await service(repository, tasks).create_event(
            repository.owner,
            title="Fixture event",
            starts_at=repository.event.starts_at,
            ends_at=repository.event.ends_at,
        )
    assert tasks.calls == []


@pytest.mark.parametrize("changed", ["user_id", "id"])
async def test_detail_does_not_accept_foreign_or_wrong_calendar_projection(changed: str) -> None:
    repository, tasks = Repository(), Tasks()
    expected = repository.event.id
    repository.event = repository.event.model_copy(update={changed: uuid4()})
    with pytest.raises(LookupError):
        await service(repository, tasks)._view_with_reminder(repository.owner, expected)
    assert tasks.calls == []


@pytest.mark.parametrize("changed", ["user_id", "source_ref", "status"])
async def test_detail_only_attaches_matching_owned_active_reminder(changed: str) -> None:
    repository, tasks = Repository(), Tasks()
    value = task(repository.event)
    tasks.views = [
        value.model_copy(
            update={
                changed: {
                    "user_id": uuid4(),
                    "source_ref": "calendar:other",
                    "status": TaskStatus.DONE,
                }[changed]
            }
        )
    ]
    view = await service(repository, tasks)._view_with_reminder(
        repository.owner, repository.event.id
    )
    assert view.reminder_task_id is None


async def test_list_rechecks_owner_status_window_and_owns_nested_participants() -> None:
    repository, tasks = Repository(), Tasks()
    accepted = repository.event.model_copy(
        update={"participants": [CalendarParticipant(name="original")]}
    )
    repository.events = [
        accepted,
        accepted.model_copy(update={"id": uuid4(), "user_id": uuid4()}),
        accepted.model_copy(update={"id": uuid4(), "status": "cancelled"}),
        accepted.model_copy(update={"id": uuid4(), "starts_at": accepted.ends_at}),
    ]
    result = await service(repository, tasks).list_events(
        repository.owner, starts_from=accepted.starts_at, starts_to=accepted.ends_at
    )
    accepted.participants.clear()
    assert [value.id for value in result] == [accepted.id]
    assert [value.name for value in result[0].participants] == ["original"]
