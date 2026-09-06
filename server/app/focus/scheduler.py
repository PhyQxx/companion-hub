"""FOCUS-01 专注提醒调度器：周期评估活跃会话并经主动通道投递建议。

与 GoalReminderScheduler 同模式（asyncio task + stop event + 首 tick
延迟一个间隔）。红线：只建议不拦截；投递走 ProactiveDeliveryService，
DND/安静时段/每日预算/降频全部沿用既有闸门；同会话同类型信号受冷却
守卫，绝不刷屏。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from app.focus.analysis import (
    DEFAULT_NUDGE_COOLDOWN_MINUTES,
    FocusSignal,
    cooldown_passed,
)
from app.focus.service import FocusService

logger = logging.getLogger("app.focus")

TRIGGER_KIND_FOCUS = "focus.nudge"


class FocusDeliverer(Protocol):
    async def __call__(
        self,
        text: str,
        *,
        user_id: UUID,
        entity_id: str,
        trigger_kind: str,
        privacy_level: str,
    ) -> list[str] | None: ...


class FocusScheduler:
    def __init__(
        self,
        service: FocusService,
        *,
        interval_seconds: float = 120.0,
        cooldown_minutes: int = DEFAULT_NUDGE_COOLDOWN_MINUTES,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        deliverer: FocusDeliverer | None = None,
    ) -> None:
        self._service = service
        self._interval = interval_seconds
        self._cooldown_minutes = cooldown_minutes
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleeper or asyncio.sleep
        self._deliverer = deliverer
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.nudged_count = 0

    def set_deliverer(self, deliverer: FocusDeliverer) -> None:
        self._deliverer = deliverer

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="aria-focus-nudges")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        # 与其他调度器相同：首个 tick 前等待一个完整间隔
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("focus nudge tick failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)

    async def run_once(self) -> int:
        now = self._clock()
        delivered = 0
        for session in self._service.active_sessions():
            try:
                signals = await self._service.evaluate(session, now=now)
            except Exception:
                logger.exception("focus evaluation failed for %s", session.session_id)
                continue
            for signal in signals:
                if not cooldown_passed(
                    session,
                    signal,
                    now=now,
                    cooldown=timedelta(minutes=self._cooldown_minutes),
                ):
                    continue
                if self._deliverer is None:
                    continue
                try:
                    await self._deliverer(
                        signal.message,
                        user_id=UUID(session.user_id),
                        entity_id=f"focus:{session.session_id}",
                        trigger_kind=TRIGGER_KIND_FOCUS,
                        privacy_level="L1",
                    )
                except Exception:
                    logger.exception("focus nudge delivery failed")
                    continue
                self._service.mark_nudged(session.user_id, signal, now=now)
                self.nudged_count += 1
                delivered += 1
        return delivered


__all__ = ["TRIGGER_KIND_FOCUS", "FocusDeliverer", "FocusScheduler", "FocusSignal"]
