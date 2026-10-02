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
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select

from app.config.models import RunBudgetConfig
from app.db import AppUserRecord
from app.focus.analysis import (
    DEFAULT_NUDGE_COOLDOWN_MINUTES,
    FocusSignal,
    cooldown_passed,
)
from app.focus.service import FocusService
from app.harness.budget import BudgetDenied
from app.harness.time import utc
from app.runs.delivery import deliver_once, recover_expired_deliveries

from .delivery import FocusDeliverySource

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
        budget_loader: Callable[[], RunBudgetConfig] | None = None,
    ) -> None:
        self._service = service
        self._interval = interval_seconds
        self._cooldown_minutes = cooldown_minutes
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleeper or asyncio.sleep
        self._deliverer = deliverer
        self._budget_loader = budget_loader or RunBudgetConfig
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.nudged_count = 0
        self._dispatches: set[asyncio.Task[object]] = set()

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
        pending = set(self._dispatches)
        if task is not None:
            pending.add(task)
        pending.discard(asyncio.current_task())
        for active in pending:
            active.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

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
        caller = asyncio.current_task()
        if caller is not None:
            self._dispatches.add(caller)
        try:
            return await self._run_once()
        finally:
            if caller is not None:
                self._dispatches.discard(caller)

    async def _run_once(self) -> int:
        await recover_expired_deliveries(self._service.database)
        now = utc(self._clock())
        attempted = 0
        sessions = self._service.active_sessions()
        async with self._service.database.sessions() as session:
            owners = set(
                await session.scalars(
                    select(AppUserRecord.id).where(
                        AppUserRecord.id.in_([UUID(value.user_id) for value in sessions]),
                        AppUserRecord.status == "active",
                    )
                )
            )
        for focus_session in sessions:
            if UUID(focus_session.user_id) not in owners:
                continue
            if self._stop.is_set():
                break
            try:
                evaluation = await self._service.evaluate_snapshot(focus_session, now=now)
            except Exception as error:
                logger.warning("focus evaluation failed: %s", type(error).__name__)
                continue
            for signal in evaluation.signals:
                if self._stop.is_set() or self._deliverer is None:
                    break
                if not cooldown_passed(
                    focus_session,
                    signal,
                    now=now,
                    cooldown=timedelta(minutes=self._cooldown_minutes),
                ):
                    continue
                owner = UUID(focus_session.user_id)
                source_id = uuid5(NAMESPACE_URL, f"aria:focus:{owner}:{focus_session.session_id}")
                cycle = max(
                    0,
                    int(
                        (now - utc(focus_session.started_at)).total_seconds()
                        // max(1, self._cooldown_minutes * 60)
                    ),
                )
                source = FocusDeliverySource(
                    source_id,
                    owner,
                    self._service,
                    focus_session,
                    signal,
                    evaluation.references,
                    now,
                    cycle,
                )

                async def dispatch(
                    text: str = signal.message,
                    user_id: UUID = owner,
                    session_id: str = focus_session.session_id,
                ) -> list[str] | None:
                    assert self._deliverer is not None
                    return await self._deliverer(
                        text,
                        user_id=user_id,
                        entity_id=f"focus:{session_id}",
                        trigger_kind=TRIGGER_KIND_FOCUS,
                        privacy_level="L1",
                    )

                try:
                    admitted = await deliver_once(
                        self._service.database,
                        source_repository=source,
                        source_id=source_id,
                        user_id=owner,
                        run_id=source.run_id,
                        text=signal.message,
                        entry=TRIGGER_KIND_FOCUS,
                        config=self._budget_loader(),
                        dispatch=dispatch,
                    )
                    if admitted:
                        self.nudged_count += 1
                        attempted += 1
                except BudgetDenied as error:
                    logger.info("focus nudge stopped: %s", error.reason_code)
        return attempted


__all__ = ["TRIGGER_KIND_FOCUS", "FocusDeliverer", "FocusScheduler", "FocusSignal"]
