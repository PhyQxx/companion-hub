"""GOAL-01 目标提醒调度器：到期前/到期各提醒一次，经主动通道投递。

与 TaskScheduler 同模式：asyncio task + stop event + 首个 tick 延迟一个
间隔（避免启动即写库）。投递失败不重试——时间戳已落库即视为已提醒，
用户可通过反馈端点稍后或忽略降频。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from app.cognition import ClaimedGoalReminder
from app.cognition.store import CognitiveStore

logger = logging.getLogger("app.cognition.goals")

TRIGGER_KIND_PRE_DUE = "goal.pre_due"
TRIGGER_KIND_DUE = "goal.due"


class GoalDeliverer(Protocol):
    async def __call__(
        self,
        text: str,
        *,
        user_id: UUID,
        goal_id: UUID,
        privacy_level: str,
        trigger_kind: str,
    ) -> list[str] | None: ...


class GoalReminderScheduler:
    def __init__(
        self,
        store: CognitiveStore,
        *,
        interval_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        deliverer: GoalDeliverer | None = None,
    ) -> None:
        self._store = store
        self._interval = interval_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleeper or asyncio.sleep
        self._deliverer = deliverer
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.reminded_count = 0

    def set_deliverer(self, deliverer: GoalDeliverer) -> None:
        self._deliverer = deliverer

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="aria-goal-reminders")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        # 与 TaskScheduler 相同：首个 tick 前等待一个完整间隔
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("goal reminder tick failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)

    async def run_once(self, *, now: datetime | None = None) -> int:
        """认领并投递到期目标提醒；返回本次触发数量（测试与手动共用）。"""
        moment = now or self._clock()
        claimed = await self._store.claim_due_goal_reminders(now=moment)
        for item in claimed:
            await self._remind(item)
        return len(claimed)

    async def _remind(self, item: ClaimedGoalReminder) -> None:
        due_text = item.due_at.strftime("%m-%d %H:%M")
        if item.phase == "pre_due":
            text = f"📌 你有一个目标临近：{item.goal.title}（预计 {due_text} 到期）"
        else:
            text = f"⏰ 目标已到期：{item.goal.title}（{due_text}）"
        channels: list[str] | None = None
        if self._deliverer is not None:
            try:
                channels = await self._deliverer(
                    text,
                    user_id=item.user_id,
                    goal_id=item.goal.id,
                    privacy_level="L1",
                    trigger_kind=(
                        TRIGGER_KIND_DUE if item.phase == "due" else TRIGGER_KIND_PRE_DUE
                    ),
                )
            except Exception as error:
                logger.warning("goal reminder delivery failed: %s", error)
        self.reminded_count += 1
        if not channels:
            logger.info("goal %s reminder delivered without channel", item.goal.id)
