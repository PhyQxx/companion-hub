"""CAL-01 日历契约：事件视图、参与者与写入前预览。

v1 日历真源是 Hub 本地库（calendar_id 固定 primary）；外部日历
（CalDAV/Google）后续以 CalendarProvider 协议接入，视图契约不变。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import Field

from app.schemas.common import StrictModel


class CalendarParticipant(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    email: Annotated[str, Field(min_length=3, max_length=240)] | None = None


class CalendarEventView(StrictModel):
    id: UUID
    user_id: UUID
    calendar_id: str
    title: str
    notes: str | None = None
    starts_at: datetime
    ends_at: datetime
    all_day: bool = False
    location: str | None = None
    participants: list[CalendarParticipant] = Field(default_factory=list)
    status: str
    # 事件来源（api=本地 / caldav|google=外部镜像），供简报/回顾标注
    source: str = "api"
    reminder_task_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CalendarPreview(StrictModel):
    """写入前预览：规范化展示待写入事件与冲突，纯读不落库。"""

    title: str
    calendar_id: str
    starts_at: datetime
    ends_at: datetime
    location: str | None = None
    participants: list[CalendarParticipant] = Field(default_factory=list)
    reminder_lead_minutes: int
    conflicts: list[CalendarEventView] = Field(default_factory=list)
