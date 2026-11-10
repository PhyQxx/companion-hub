"""Periodic generation, independent of notification delivery."""

import asyncio
import contextlib
import logging
from datetime import datetime

from .activity_summary import ActivitySummaryService, Kind

logger = logging.getLogger(__name__)


class ActivitySummaryScheduler:
    def __init__(self, service: ActivitySummaryService) -> None:
        self.service = service
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="activity-summaries")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            if self._stop.is_set():
                break
            try:
                await self.run_once()
            except Exception:
                logger.exception("activity summary tick failed")

    async def run_once(self, *, now: datetime | None = None) -> None:
        for user_id in await self.service.active_user_ids():
            kinds: tuple[Kind, ...] = ("daily", "weekly")
            for kind in kinds:
                try:
                    await self.service.build(user_id, kind, now=now)
                except Exception:
                    logger.exception("activity summary generation failed for %s/%s", user_id, kind)
