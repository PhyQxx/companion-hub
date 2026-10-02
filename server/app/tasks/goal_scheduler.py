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
from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, CognitiveGoalRecord
from app.harness.budget import BudgetDenied
from app.ids import uuid7
from app.runs.delivery import (
    GoalDeliveryClaim,
    deliver_once,
    goal_delivery_text,
    recover_expired_deliveries,
)

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
        budget_loader: Callable[[], RunBudgetConfig] | None = None,
    ) -> None:
        self._store = store
        self._interval = interval_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleeper or asyncio.sleep
        self._deliverer = deliverer
        self._budget_loader = budget_loader or RunBudgetConfig
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.reminded_count = 0
        self._dispatches: set[asyncio.Task[object]] = set()

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
        pending = set(self._dispatches)
        if task is not None:
            pending.add(task)
        pending.discard(asyncio.current_task())
        for active in pending:
            active.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

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
        await recover_expired_deliveries(self._store.database)
        count = 0
        for _ in range(20):
            if self._stop.is_set():
                break
            claimed = await self._store.claim_due_goal_reminders(now=now or self._clock(), limit=1)
            if not claimed:
                break
            await self._remind(claimed[0])
            count += 1
        return count

    async def _remind(self, item: ClaimedGoalReminder) -> None:
        current = asyncio.current_task()
        if current is not None:
            self._dispatches.add(current)
        try:
            if self._stop.is_set() or item.claimed_at is None:
                return
            if not await self._store.reminder_claim_visible(item):
                return
            async with self._store.database.sessions() as session:
                owner = await session.get(AppUserRecord, item.user_id)
                if owner is None or owner.status != "active":
                    return
                timezone = owner.timezone
            claim = GoalDeliveryClaim(item.phase, item.claimed_at, timezone)
            text = goal_delivery_text(item.goal.title, item.due_at, claim)
            trigger = TRIGGER_KIND_DUE if item.phase == "due" else TRIGGER_KIND_PRE_DUE

            async def dispatch() -> list[str] | None:
                if self._deliverer is None:
                    return None
                return await self._deliverer(
                    text,
                    user_id=item.user_id,
                    goal_id=item.goal.id,
                    privacy_level="L1",
                    trigger_kind=trigger,
                )

            try:
                admitted = await deliver_once(
                    self._store.database,
                    table=CognitiveGoalRecord,
                    source_id=item.goal.id,
                    user_id=item.user_id,
                    text=text,
                    entry=trigger,
                    config=self._budget_loader(),
                    dispatch=dispatch,
                    run_id=uuid7(),
                    goal_claim=claim,
                    unavailable_reason="no_deliverer" if self._deliverer is None else None,
                )
                if admitted:
                    self.reminded_count += 1
            except BudgetDenied as error:
                logger.info("goal reminder stopped: %s", error.reason_code)
        finally:
            if current is not None:
                self._dispatches.discard(current)
