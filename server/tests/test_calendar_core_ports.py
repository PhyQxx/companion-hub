"""Pure calendar ports preserve interruption and compatibility behavior."""

import asyncio
from datetime import timedelta
from typing import Any, cast

import pytest
from test_calendar_coordinator_boundary import Repository, Tasks
from test_commute_query_boundary import NOW

from app.calendar.models import CalendarEventView
from app.calendar.ports import CalendarReminderWriter, CalendarRepository
from app.calendar.service import _ref as legacy_ref
from app.calendar.service import _validate_lead as legacy_lead
from app.calendar.service import _validate_window as legacy_window
from app.calendar.service_core import CalendarCoordinator, _ref, _validate_lead, _validate_window
from scripts.check_architecture import allowed


@pytest.mark.parametrize(
    "stage", ["preview", "create", "update", "cancel", "get", "list", "write_task", "read_task"]
)
@pytest.mark.parametrize("cancel", [False, True])
async def test_port_failure_or_cancellation_is_not_retried(stage: str, cancel: bool) -> None:
    calls: list[str] = []

    def fail(name: str) -> None:
        calls.append(name)
        if name == stage:
            if cancel:
                raise asyncio.CancelledError
            raise RuntimeError("synthetic port failure")

    class BrokenRepository(Repository):
        async def overlapping(self, user_id: object, **kwargs: Any) -> list[CalendarEventView]:
            fail("preview")
            return await super().overlapping(user_id, **kwargs)

        async def create_event(self, **kwargs: Any) -> CalendarEventView:
            fail("create")
            return await super().create_event(**kwargs)

        async def update_event(
            self, user_id: object, event_id: object, **kwargs: Any
        ) -> CalendarEventView:
            fail("update")
            return await super().update_event(user_id, event_id, **kwargs)

        async def cancel_event(self, user_id: object, event_id: object) -> CalendarEventView:
            fail("cancel")
            return await super().cancel_event(user_id, event_id)

        async def get_event(self, user_id: object, event_id: object) -> CalendarEventView:
            fail("get")
            return await super().get_event(user_id, event_id)

        async def list_events(self, user_id: object, **kwargs: Any) -> list[CalendarEventView]:
            fail("list")
            return await super().list_events(user_id, **kwargs)

    class BrokenTasks(Tasks):
        async def replace_calendar_reminder(
            self, user_id: object, event: CalendarEventView, **kwargs: Any
        ) -> Any:
            fail("write_task")
            return await super().replace_calendar_reminder(user_id, event, **kwargs)

        async def find_active_by_source_ref(self, user_id: object, source_ref: str) -> Any:
            fail("read_task")
            return await super().find_active_by_source_ref(user_id, source_ref)

    repository, tasks = BrokenRepository(), BrokenTasks()
    core = CalendarCoordinator(
        cast(CalendarRepository, repository), cast(CalendarReminderWriter, tasks), clock=lambda: NOW
    )
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        if stage == "preview":
            await core.preview(
                repository.owner,
                title="Fixture",
                starts_at=repository.event.starts_at,
                ends_at=repository.event.ends_at,
            )
        elif stage in {"create", "write_task"}:
            await core.create_event(
                repository.owner,
                title="Fixture",
                starts_at=repository.event.starts_at,
                ends_at=repository.event.ends_at,
            )
        elif stage == "update":
            await core.reschedule_event(repository.owner, repository.event.id)
        elif stage == "cancel":
            await core.cancel_event(repository.owner, repository.event.id)
        elif stage == "list":
            await core.list_events(repository.owner)
        else:
            await core._view_with_reminder(repository.owner, repository.event.id)
    assert calls == (
        {"write_task": ["create", "write_task"], "read_task": ["get", "read_task"]}.get(
            stage, [stage]
        )
    )


@pytest.mark.parametrize("lead", [0, 10, 1440])
async def test_pure_core_keeps_existing_reminder_lead_behavior(lead: int) -> None:
    repository, tasks = Repository(), Tasks()
    core = CalendarCoordinator(
        cast(CalendarRepository, repository), cast(CalendarReminderWriter, tasks), clock=lambda: NOW
    )
    await core.create_event(
        repository.owner,
        title="Fixture",
        starts_at=NOW + timedelta(hours=2),
        ends_at=NOW + timedelta(hours=3),
        reminder_lead_minutes=lead,
    )
    assert len(tasks.calls) == (0 if lead == 0 else 1)
    assert len(repository.writes) == 1


@pytest.mark.parametrize("module", ["app.calendar.service_core", "app.calendar.ports"])
@pytest.mark.parametrize(
    "target",
    [
        "sqlalchemy",
        "httpx",
        "requests",
        "openai",
        "anthropic",
        "asyncpg",
        "aiosqlite",
        "app.db",
        "app.calendar.store",
        "app.tasks.store",
        "app.llm.router",
        "app.api",
    ],
)
def test_calendar_core_cannot_import_persistence_or_provider_adapters(
    module: str, target: str
) -> None:
    assert not allowed(module, target)


def test_legacy_validation_helpers_remain_the_same_functions() -> None:
    assert (
        legacy_ref is _ref and legacy_lead is _validate_lead and legacy_window is _validate_window
    )
