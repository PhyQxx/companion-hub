"""REVIEW-01 晚间回顾调度器：本地时间到达夜间配置时刻后为每个活跃用户投递。

与 DailyBriefScheduler 同模式；每晚至多一次由 daily_review 的
(user_id, review_date) 唯一约束与 pending→delivered 乐观转移保证。
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

from .review import DailyReviewService

logger = logging.getLogger("app.tasks.review")


def _parse_review_time(raw: str) -> dt_time:
    hour, minute = raw.split(":", 1)
    return dt_time(int(hour), int(minute))


class ReviewDelivererProtocol(Protocol):
    async def __call__(
        self,
        text: str,
        *,
        user_id: UUID,
        review_id: UUID,
        privacy_level: PrivacyLevel,
        trigger_kind: str,
    ) -> list[str] | None: ...


class DailyReviewScheduler:
    def __init__(
        self,
        service: DailyReviewService,
        *,
        interval_seconds: float = 60.0,
        review_time: dt_time | None = None,
        timezone_name: str = "Asia/Shanghai",
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        deliverer: ReviewDelivererProtocol | None = None,
    ) -> None:
        self._service = service
        self._interval = interval_seconds
        self._review_time = review_time or dt_time(21, 30)
        self._tz = ZoneInfo(timezone_name)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleeper or asyncio.sleep
        self._deliverer = deliverer
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.delivered_count = 0

    def set_deliverer(self, deliverer: ReviewDelivererProtocol) -> None:
        self._deliverer = deliverer

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="aria-daily-review")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        # 与其余调度器一致：首个 tick 前等待一个完整间隔
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("daily review tick failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)

    async def run_once(self, *, now: datetime | None = None) -> int:
        """到达回顾时刻后为所有活跃用户补齐当晚回顾；返回本轮投递数。"""
        moment = now or self._clock()
        local = moment.astimezone(self._tz)
        if (local.hour, local.minute) < (self._review_time.hour, self._review_time.minute):
            return 0
        delivered = 0
        for user_id in await self._service.active_user_ids():
            review = await self._service.get_review(user_id, local.date())
            if review is not None and review.status == "delivered":
                continue
            if self._deliverer is None:
                continue
            try:
                result = await self._service.deliver(
                    user_id, deliverer=self._deliverer, review_date=local.date()
                )
            except Exception:
                logger.exception("daily review failed for %s", user_id)
                continue
            if result.status == "delivered":
                delivered += 1
        self.delivered_count += delivered
        return delivered
