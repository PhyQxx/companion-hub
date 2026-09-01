"""BRIEF-01 简报调度器：本地时间到达配置时刻后，为每个活跃用户生成并投递当日简报。

每天至多一次由 daily_brief 的 (user_id, brief_date) 唯一约束与
pending→delivered 乐观转移保证；首个 tick 前同样等待一个完整间隔。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from datetime import time as dt_time
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from app.schemas.common import PrivacyLevel

from .brief import DailyBriefService

logger = logging.getLogger("app.tasks.brief")


def _parse_brief_time(raw: str) -> dt_time:
    hour, minute = raw.split(":", 1)
    return dt_time(int(hour), int(minute))


class DailyBriefScheduler:
    def __init__(
        self,
        service: DailyBriefService,
        *,
        interval_seconds: float = 60.0,
        brief_time: dt_time | None = None,
        timezone_name: str = "Asia/Shanghai",
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        deliverer: BriefDelivererProtocol | None = None,
    ) -> None:
        self._service = service
        self._interval = interval_seconds
        self._brief_time = brief_time or dt_time(8, 0)
        self._tz = ZoneInfo(timezone_name)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleeper or asyncio.sleep
        self._deliverer = deliverer
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.delivered_count = 0

    def set_deliverer(self, deliverer: BriefDelivererProtocol) -> None:
        self._deliverer = deliverer

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="aria-daily-brief")

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
                await self.run_once()
            except Exception:
                logger.exception("daily brief tick failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)

    async def run_once(self, *, now: datetime | None = None) -> int:
        """到达投递时刻后为所有活跃用户补齐当日简报；返回本轮投递数。"""
        moment = now or self._clock()
        local = moment.astimezone(self._tz)
        if (local.hour, local.minute) < (self._brief_time.hour, self._brief_time.minute):
            return 0
        delivered = 0
        for user_id in await self._service.active_user_ids():
            brief = await self._service.get_brief(user_id, local.date())
            if brief is not None and brief.status == "delivered":
                continue
            if self._deliverer is None:
                continue
            try:
                result = await self._service.deliver(
                    user_id, deliverer=self._deliverer, brief_date=local.date()
                )
            except Exception:
                logger.exception("daily brief failed for %s", user_id)
                continue
            if result.status == "delivered":
                delivered += 1
        self.delivered_count += delivered
        return delivered


class BriefDelivererProtocol(Protocol):
    async def __call__(
        self,
        text: str,
        *,
        user_id: UUID,
        brief_id: UUID,
        privacy_level: PrivacyLevel,
        trigger_kind: str,
    ) -> list[str] | None: ...
