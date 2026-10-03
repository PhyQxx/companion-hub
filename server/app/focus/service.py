"""Compatibility facade binding the focus core to durable delivery storage."""

from collections.abc import Callable
from datetime import datetime

from app.db import Database
from app.timeline.store import TimelineStore

from .analysis import DEFAULT_LONG_WORK_MINUTES, DEFAULT_SWITCH_COUNT, DEFAULT_SWITCH_WINDOW_MINUTES
from .core import MAX_SESSION_MINUTES as MAX_SESSION_MINUTES
from .core import FocusEvaluation as FocusEvaluation
from .core import FocusSessionService


class FocusService(FocusSessionService):
    def __init__(
        self,
        timeline_store: TimelineStore,
        *,
        long_work_minutes: int = DEFAULT_LONG_WORK_MINUTES,
        switch_window_minutes: int = DEFAULT_SWITCH_WINDOW_MINUTES,
        switch_count: int = DEFAULT_SWITCH_COUNT,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            timeline_store,
            long_work_minutes=long_work_minutes,
            switch_window_minutes=switch_window_minutes,
            switch_count=switch_count,
            clock=clock,
        )
        self._database = timeline_store.database

    @property
    def database(self) -> Database:
        return self._database


__all__ = ["MAX_SESSION_MINUTES", "FocusService"]
