"""TASK-01 任务调度器：到期轮询、事件触发与主动投递。

照 ScreenAwarenessLoop 模式：asyncio task + stop event；投递复用
ProactiveDeliveryService（Web 私聊 / 桌面通知 / 在线语音按通道配置仲裁）。
提醒是用户显式请求的内容，不消耗主动每日预算；通道启停与隐私上限仍生效。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from app.schemas.common import PrivacyLevel

from .models import ClaimedTask
from .store import TaskStore

logger = logging.getLogger("app.tasks")

TRIGGER_KIND_TIME = "task.due"
TRIGGER_KIND_EVENT = "task.event"


class TaskDeliverer(Protocol):
    async def __call__(
        self,
        text: str,
        *,
        user_id: UUID,
        task_id: UUID,
        privacy_level: PrivacyLevel,
        trigger_kind: str,
    ) -> list[str] | None: ...


class SemanticEventLike(Protocol):
    user_id: UUID
    kind: str


class TaskScheduler:
    def __init__(
        self,
        store: TaskStore,
        *,
        interval_seconds: float = 15.0,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        deliverer: TaskDeliverer | None = None,
    ) -> None:
        self._store = store
        self._interval = interval_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleeper or asyncio.sleep
        self._deliverer = deliverer
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.recovered_count = 0
        self.fired_count = 0

    def set_deliverer(self, deliverer: TaskDeliverer) -> None:
        self._deliverer = deliverer

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="aria-task-scheduler")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        # 首个 tick 前先等一个完整间隔：既符合提醒的触发粒度，也避免进程
        # 启动即写库——在 SQLite :memory: 测试环境里与短暂请求事务并发会
        # 让连接池分裂出第二个空连接。
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
        try:
            self.recovered_count = await self._store.recover_interrupted()
            if self.recovered_count:
                logger.info("recovered %d interrupted task firings", self.recovered_count)
        except Exception:
            logger.exception("task recovery failed")
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("task scheduler tick failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)

    async def run_once(self, *, now: datetime | None = None) -> int:
        """认领并投递所有到期任务；返回本 tick 触发数量（测试与手动触发共用）。"""
        moment = now or self._clock()
        claimed = await self._store.claim_due(now=moment)
        for task in claimed:
            await self._fire(task, trigger_kind=TRIGGER_KIND_TIME)
        return len(claimed)

    async def on_semantic_event(self, event: SemanticEventLike) -> None:
        """Perception 管线事件观察口：匹配事件触发任务并投递（异常不外溢）。"""
        try:
            claimed = await self._store.claim_event(event.user_id, event.kind)
            for task in claimed:
                await self._fire(task, trigger_kind=TRIGGER_KIND_EVENT)
        except Exception:
            logger.exception("task event trigger failed for %s", event.kind)

    async def _fire(self, task: ClaimedTask, *, trigger_kind: str) -> None:
        text = f"⏰ 提醒：{task.title}"
        if task.notes:
            text = f"{text}\n{task.notes}"
        channels: list[str] | None = None
        reason_code: str | None = None
        if self._deliverer is None:
            reason_code = "no_deliverer"
        else:
            try:
                channels = await self._deliverer(
                    text,
                    user_id=task.user_id,
                    task_id=task.id,
                    privacy_level=task.privacy_level,
                    trigger_kind=trigger_kind,
                )
            except Exception as error:
                reason_code = f"deliver_error:{type(error).__name__}"
                logger.warning("task delivery failed: %s", error)
        if not channels and reason_code is None:
            reason_code = "no_available_channel"
        delivery: dict[str, Any] = {
            "fired_at": task.fired_at.isoformat(),
            "trigger_kind": trigger_kind,
            "channels": channels or [],
            "reason_code": reason_code,
        }
        await self._store.finish_firing(task.id, delivery=delivery)
        self.fired_count += 1
        if not channels:
            logger.info("task %s fired without delivery (%s)", task.id, reason_code)
