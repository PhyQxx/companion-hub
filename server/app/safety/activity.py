"""SAFE-01 久未活动检测：多信号最后活动时间 + 生效时段 + 确定性提醒。

活动信号（任一即刷新）：聊天/语音回合、桌面设备心跳（device.last_seen_at）。
超阈值且在生效时段内 → 经认知闸门投递"久未活动"提醒（每阈值周期最多一次）。
只依赖在场/交互信号，不做摄像头/音频分析（docs/44 §5）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.cognition import CognitiveCycle, CognitiveDecision, DecisionKind, SemanticEvent
from app.db import AppUserRecord, Database, DeviceClientRecord
from app.ids import uuid7
from app.output import ProactiveDeliveryResult
from app.schemas import PrivacyLevel

logger = logging.getLogger("app.safety.activity")

DeliveryResult = ProactiveDeliveryResult | None
Deliver = Callable[..., Awaitable[DeliveryResult | None]]
Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]

# 重复提醒冷却：同一轮"没回应"后不要连续追问
REMIND_COOLDOWN = timedelta(hours=6)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _parse_range(text: str) -> tuple[time, time]:
    start_text, end_text = text.split("-", 1)
    return time.fromisoformat(start_text), time.fromisoformat(end_text)


class ActivityTracker:
    """内存活动记录：外部信号源显式上报，查询时与设备心跳取最大。"""

    def __init__(self) -> None:
        self._last_activity: dict[UUID, datetime] = {}

    def record(self, user_id: UUID, at: datetime | None = None) -> None:
        self._last_activity[user_id] = _aware(at or datetime.now(UTC))

    def last_known(self, user_id: UUID) -> datetime | None:
        return self._last_activity.get(user_id)


class SafetyActivityScheduler:
    def __init__(
        self,
        database: Database,
        config_store: Any,
        deliver: Deliver,
        tracker: ActivityTracker,
        *,
        cognitive_cycle: CognitiveCycle | None = None,
        clock: Clock | None = None,
        sleeper: Sleeper | None = None,
    ) -> None:
        self._database = database
        self._config_store = config_store
        self._deliver = deliver
        self._tracker = tracker
        self._cognitive_cycle = cognitive_cycle
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleeper or asyncio.sleep
        self._last_reminded: dict[UUID, datetime] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="aria-safety-activity")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while not self._stop.is_set():
            interval = 300.0
            try:
                config = self._config_store.current.config.safety
                await self._tick(config)
            except Exception:
                logger.exception("safety activity tick failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=interval)

    async def _tick(self, config: Any) -> None:
        if not config.enabled:
            return
        now = self._clock()
        user_id = await self._active_user_id()
        if user_id is None:
            return
        timezone = await self._user_timezone()
        local_now = now.astimezone(timezone)
        start, end = _parse_range(config.inactivity_active_range)
        # 支持跨夜窗口（如 09:00-22:00 不跨；22:00-06:00 跨）
        if start <= end:
            in_range = start <= local_now.time() <= end
        else:
            in_range = local_now.time() >= start or local_now.time() <= end
        if not in_range:
            return
        threshold = timedelta(hours=float(config.inactivity_hours))
        last = await self._last_activity_at(user_id)
        if last is None or now - last < threshold:
            return
        last_reminded = self._last_reminded.get(user_id)
        if (
            last_reminded is not None
            and now - last_reminded < REMIND_COOLDOWN
        ):
            return
        hours_text = f"{float(config.inactivity_hours):g} 小时"
        message = (
            # 面向用户的中文文案保留全角标点
            f"已经 {hours_text} 没有你的任何活动记录了，一切都好吗？"  # noqa: RUF001
            f"（最后活动：{last.astimezone(timezone).strftime('%m-%d %H:%M')}）"
        )
        decision: CognitiveDecision | None = None
        if self._cognitive_cycle is not None:
            decision = await self._cognitive_cycle.evaluate(
                SemanticEvent(
                    event_id=uuid7(),
                    user_id=user_id,
                    kind="user.inactive",
                    source_kind="system",
                    dedupe_key=f"user.inactive:{local_now.date()}",
                    summary=f"用户超过 {hours_text} 无活动",
                    occurred_at=now,
                    privacy_level=PrivacyLevel.L1,
                    confidence=0.7,
                    evidence_ids=[f"device:last_seen:{last.isoformat()}"],
                    attributes={"message": message},
                    expires_at=now + timedelta(minutes=30),
                )
            )
            if decision.decision in {DecisionKind.IGNORE, DecisionKind.RECORD}:
                return
            message = decision.message or message
        result = await self._deliver(
            message,
            entity_id="safety:activity",
            rule_id="user_inactive",
            trigger_kind="user.inactive",
            privacy_level=PrivacyLevel.L1,
            cognitive_decision=decision,
            target_user_id=user_id,
        )
        if result is not None:
            self._last_reminded[user_id] = now

    async def _last_activity_at(self, user_id: UUID) -> datetime | None:
        candidates: list[datetime] = []
        tracked = self._tracker.last_known(user_id)
        if tracked is not None:
            candidates.append(tracked)
        async with self._database.sessions() as session:
            owner_exists = await session.scalar(
                select(AppUserRecord.id).where(AppUserRecord.id == user_id)
            )
            if owner_exists is not None:
                last_seen = await session.scalar(
                    select(DeviceClientRecord.last_seen_at)
                    .where(
                        DeviceClientRecord.owner_user_id == user_id,
                        DeviceClientRecord.revoked_at.is_(None),
                    )
                    .order_by(DeviceClientRecord.last_seen_at.desc())
                    .limit(1)
                )
                if last_seen is not None:
                    candidates.append(_aware(last_seen))
        return max(candidates) if candidates else None

    async def _active_user_id(self) -> UUID | None:
        async with self._database.sessions() as session:
            value = await session.scalar(
                select(AppUserRecord.id)
                .where(AppUserRecord.status == "active")
                .order_by(AppUserRecord.created_at)
                .limit(1)
            )
        return value if isinstance(value, UUID) else None

    async def _user_timezone(self) -> Any:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        async with self._database.sessions() as session:
            name = await session.scalar(
                select(AppUserRecord.timezone)
                .where(AppUserRecord.status == "active")
                .order_by(AppUserRecord.created_at)
                .limit(1)
            )
        try:
            return ZoneInfo(name or "Asia/Shanghai")
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("Asia/Shanghai")
