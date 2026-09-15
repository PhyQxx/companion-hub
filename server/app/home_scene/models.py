"""HOME-01 家庭场景契约：场景视图与触发结果。

红线（docs/00「每个场景有条件、计划、确认和退出条件」）：场景定义必须
带触发器与可选生效时段（条件），触发只展开为待确认行动计划（计划+
确认），退出由用户取消计划或关闭场景实现——场景层绝不直接执行设备
动作。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue

from app.schemas.common import StrictModel, TokenName

MAX_SCENE_STEPS = 10
MANUAL_TRIGGER = "manual"


class HomeSceneStep(StrictModel):
    action_id: TokenName
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class HomeSceneView(StrictModel):
    id: UUID
    user_id: UUID
    name: str
    trigger: str
    window_start: str | None = None
    window_end: str | None = None
    steps: list[HomeSceneStep] = Field(default_factory=list)
    enabled: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


class HomeSceneTriggered(StrictModel):
    scene_id: UUID
    scene_name: str
    plan_id: UUID
    plan_status: str
    awaiting_confirmation: bool


TriggerKind = Literal["perception", "manual"]


def normalize_window(value: str) -> str:
    """校验 HH:MM 时段边界。"""
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError("时段必须为 HH:MM")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as error:
        raise ValueError("时段必须为 HH:MM") from error
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("时段必须为 HH:MM")
    return f"{hour:02d}:{minute:02d}"


def in_window(now: datetime, window_start: str | None, window_end: str | None) -> bool:
    """场景生效时段判定；未配置表示全天。支持跨夜（21:00-07:00）。"""
    if window_start is None or window_end is None:
        return True
    start_hour, start_minute = (int(part) for part in window_start.split(":"))
    end_hour, end_minute = (int(part) for part in window_end.split(":"))
    current = now.hour * 60 + now.minute
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    if start <= end:
        return start <= current < end
    return current >= start or current < end


__all__ = [
    "MANUAL_TRIGGER",
    "MAX_SCENE_STEPS",
    "HomeSceneStep",
    "HomeSceneTriggered",
    "HomeSceneView",
    "in_window",
    "normalize_window",
]
