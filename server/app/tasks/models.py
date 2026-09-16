"""TASK-01 提醒与计划任务的触发契约、视图与下次触发计算。

周期复现基于锚点时间（`at`）做日期算术：默认时区（Asia/Shanghai）无夏令时，
锚点 + N 天可稳定保持每日墙上时间；跨夏令时时区会在切换日偏移一小时。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from app.schemas.common import PrivacyLevel, StrictModel

Weekday = Annotated[int, Field(ge=0, le=6)]


class TaskKind(StrEnum):
    REMINDER = "reminder"
    TASK = "task"


class TaskStatus(StrEnum):
    ACTIVE = "active"
    FIRING = "firing"
    DONE = "done"
    CANCELLED = "cancelled"


class RepeatKind(StrEnum):
    ONCE = "once"
    DAILY = "daily"
    WEEKDAYS = "weekdays"
    WEEKLY = "weekly"
    INTERVAL = "interval_minutes"


class TaskTrigger(StrictModel):
    """time 触发：`at` 为首次触发时间（周期任务同时作为复现锚点）。

    event 触发：`event_type` 精确匹配 Perception 管线的 SemanticEvent.kind
    （如 `user_arrived_home`），`cooldown_seconds` 限制同一事件的最短重触发间隔。
    """

    type: Literal["time", "event"]
    at: datetime | None = None
    repeat_kind: RepeatKind = RepeatKind.ONCE
    interval_minutes: Annotated[int, Field(ge=1, le=525_600)] | None = None
    weekdays: Annotated[list[Weekday], Field(max_length=7)] | None = None
    event_type: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    cooldown_seconds: Annotated[int, Field(ge=60, le=86_400)] | None = None


class TaskView(StrictModel):
    id: UUID
    user_id: UUID
    kind: TaskKind
    title: str
    notes: str | None = None
    status: TaskStatus
    trigger: TaskTrigger
    next_fire_at: datetime | None = None
    last_fired_at: datetime | None = None
    fire_count: int = 0
    last_delivery: dict[str, object] | None = None
    privacy_level: PrivacyLevel
    source: str
    source_ref: str | None = None
    # TODO-01 外部镜像投影（pnkx 为单一真源时的本地可见字段）
    priority: int | None = None
    group_label: str | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ClaimedTask(StrictModel):
    """已被当前执行方原子认领的一次触发；oneshot 任务投递结束后需转 done。"""

    id: UUID
    user_id: UUID
    kind: TaskKind
    title: str
    notes: str | None = None
    oneshot: bool
    privacy_level: PrivacyLevel
    fired_at: datetime


DEFAULT_EVENT_COOLDOWN_SECONDS = 300
_MAX_ADVANCE_STEPS = 100_000


def validate_trigger(trigger: TaskTrigger, *, now: datetime) -> TaskTrigger:
    """创建时校验触发器组合，返回规范化后的触发器。"""
    if trigger.type == "event":
        if not trigger.event_type:
            raise ValueError("event 触发必须提供 event_type")
        if trigger.at is not None or trigger.repeat_kind != RepeatKind.ONCE:
            raise ValueError("event 触发不支持 at 或 repeat")
        if trigger.cooldown_seconds is None:
            return trigger.model_copy(update={"cooldown_seconds": DEFAULT_EVENT_COOLDOWN_SECONDS})
        return trigger
    if trigger.event_type is not None or trigger.cooldown_seconds is not None:
        raise ValueError("time 触发不支持 event_type 或 cooldown_seconds")
    if trigger.at is None:
        raise ValueError("time 触发必须提供 at")
    if trigger.repeat_kind == RepeatKind.INTERVAL:
        if trigger.interval_minutes is None:
            raise ValueError("interval_minutes 周期必须提供 interval_minutes")
    elif trigger.interval_minutes is not None:
        raise ValueError("interval_minutes 仅用于 interval_minutes 周期")
    if trigger.repeat_kind == RepeatKind.WEEKLY:
        if not trigger.weekdays:
            raise ValueError("weekly 周期必须提供 weekdays")
        if len(set(trigger.weekdays)) != len(trigger.weekdays):
            raise ValueError("weekdays 不能重复")
    elif trigger.weekdays is not None:
        raise ValueError("weekdays 仅用于 weekly 周期")
    if trigger.repeat_kind == RepeatKind.ONCE and trigger.at <= now:
        raise ValueError("一次性提醒的触发时间必须在未来")
    return trigger


def compute_next_fire(trigger: TaskTrigger, *, after: datetime) -> datetime | None:
    """计算 `after` 之后（严格大于）的下一次触发时间；一次性任务返回 None。

    错过的周期触发不补发：从 `after` 起找下一个未来 occurrence。
    """
    if trigger.type != "time" or trigger.repeat_kind == RepeatKind.ONCE:
        return None
    anchor = trigger.at
    if anchor is None:
        return None
    anchor = _aware(anchor)
    after = _aware(after)
    kind = trigger.repeat_kind
    if kind == RepeatKind.INTERVAL:
        assert trigger.interval_minutes is not None
        step = timedelta(minutes=trigger.interval_minutes)
        elapsed = after - anchor
        periods = elapsed // step
        candidate = anchor + step * max(periods + 1, 1)
        if candidate <= after:  # pragma: no cover - 整除边界保护
            candidate += step
        return candidate
    allowed: set[int] | None = None
    if kind == RepeatKind.WEEKDAYS:
        allowed = {0, 1, 2, 3, 4}
    elif kind == RepeatKind.WEEKLY:
        assert trigger.weekdays is not None
        allowed = set(trigger.weekdays)
    candidate = anchor + timedelta(days=max((after - anchor).days, 0))
    if candidate <= after:
        candidate += timedelta(days=1)
    if allowed is None:
        return candidate
    for _ in range(_MAX_ADVANCE_STEPS):
        if candidate.weekday() in allowed:
            return candidate
        candidate += timedelta(days=1)
    return None  # pragma: no cover - 一周内必命中


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
