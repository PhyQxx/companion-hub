"""Calendar coordination depends on owned projections and source-fenced writes."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.tasks.models import TaskTrigger, TaskView

from .models import CalendarEventView, CalendarParticipant


class CalendarRepository(Protocol):
    async def get_event(self, user_id: UUID, event_id: UUID) -> CalendarEventView: ...

    async def create_event(
        self,
        *,
        user_id: UUID,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        calendar_id: str = "primary",
        notes: str | None = None,
        location: str | None = None,
        participants: list[CalendarParticipant] | None = None,
    ) -> CalendarEventView: ...

    async def update_event(
        self,
        user_id: UUID,
        event_id: UUID,
        *,
        title: str | None = None,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
        location: str | None = None,
        notes: str | None = None,
    ) -> CalendarEventView: ...

    async def cancel_event(self, user_id: UUID, event_id: UUID) -> CalendarEventView: ...

    async def list_events(
        self,
        user_id: UUID,
        *,
        starts_from: datetime | None = None,
        starts_to: datetime | None = None,
        include_cancelled: bool = False,
        limit: int = 200,
    ) -> list[CalendarEventView]: ...

    async def overlapping(
        self, user_id: UUID, *, starts_at: datetime, ends_at: datetime
    ) -> list[CalendarEventView]: ...


class CalendarReminderWriter(Protocol):
    async def replace_calendar_reminder(
        self,
        user_id: UUID,
        event: CalendarEventView,
        *,
        title: str,
        trigger: TaskTrigger,
        source: str,
        now: datetime,
    ) -> TaskView: ...

    async def find_active_by_source_ref(
        self, user_id: UUID, source_ref: str
    ) -> TaskView | None: ...
