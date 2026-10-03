"""Compatibility composition for SQL calendar projections and source-fenced tasks."""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from app.tasks.store import TaskStore

from .models import CalendarEventView
from .service_core import DEFAULT_REMINDER_LEAD_MINUTES as DEFAULT_REMINDER_LEAD_MINUTES
from .service_core import MAX_REMINDER_LEAD_MINUTES as MAX_REMINDER_LEAD_MINUTES
from .service_core import MIN_REMINDER_LEAD_MINUTES as MIN_REMINDER_LEAD_MINUTES
from .service_core import CalendarCoordinator
from .service_core import _ref as _ref
from .service_core import _validate_lead as _validate_lead
from .service_core import _validate_window as _validate_window
from .store import CalendarStore, _to_view


class _CalendarRepository:
    def __init__(self, store: CalendarStore) -> None:
        self._store = store
        self.create_event = store.create_event
        self.update_event = store.update_event
        self.cancel_event = store.cancel_event
        self.list_events = store.list_events
        self.overlapping = store.overlapping

    async def get_event(self, user_id: UUID, event_id: UUID) -> CalendarEventView:
        return _to_view(await self._store.get_event(user_id, event_id))


class CalendarService(CalendarCoordinator):
    def __init__(
        self,
        store: CalendarStore,
        task_store: TaskStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(_CalendarRepository(store), task_store, clock=clock)
        self._legacy_store = store
        self._legacy_tasks = task_store

    @property
    def store(self) -> CalendarStore:
        return self._legacy_store

    @property
    def task_store(self) -> TaskStore:
        return self._legacy_tasks
