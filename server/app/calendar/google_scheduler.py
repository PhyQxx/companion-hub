"""CAL-01 Google 日历同步调度器：按间隔执行 GoogleCalendarSyncService.sync_once。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from .google import GoogleCalendarSyncService, SyncStats

logger = logging.getLogger("app.calendar.google")


class GoogleCalendarSyncScheduler:
    def __init__(
        self,
        service: GoogleCalendarSyncService,
        *,
        interval_seconds: float = 900.0,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._service = service
        self._interval = interval_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleeper or asyncio.sleep
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.last_stats: SyncStats | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="aria-google-calendar-sync")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
        while not self._stop.is_set():
            try:
                self.last_stats = await self._service.sync_once()
            except Exception:
                logger.exception("google calendar sync tick failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
