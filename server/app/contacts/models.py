"""CONTACT-01 联系人契约：名称、别名、时区、重要日期与授权偏好。

红线（对应验收「不自动推断敏感关系属性」）：relationship 与 preferences
只来自用户的显式陈述，本模块与工具层都不做任何后台推断；写入路径只有
用户 API 与用户明确请求的聊天工具。
"""

from __future__ import annotations

import calendar
from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from pydantic import Field, model_validator

from app.schemas.common import StrictModel


class ContactImportantDate(StrictModel):
    """年度重复的重要日期（生日/纪念日等）；year 可选用于精确年龄。"""

    label: Annotated[str, Field(min_length=1, max_length=40)]
    month: Annotated[int, Field(ge=1, le=12)]
    day: Annotated[int, Field(ge=1, le=31)]
    year: Annotated[int, Field(ge=1000, le=3000)] | None = None

    @model_validator(mode="after")
    def validate_calendar_day(self) -> ContactImportantDate:
        # 无 year 时按闰年二月校验（允许 2/29）；有 year 按真实年历校验。
        days = calendar.monthrange(self.year if self.year is not None else 2000, self.month)[1]
        if self.day > days:
            raise ValueError("important date day is invalid for month")
        return self


class ContactPreference(StrictModel):
    """用户明确授权记录的单条偏好（如「不爱吃香菜」「只发短信」）。"""

    key: Annotated[str, Field(min_length=1, max_length=60)]
    value: Annotated[str, Field(min_length=1, max_length=240)]
    note: Annotated[str, Field(max_length=240)] | None = None


class ContactView(StrictModel):
    id: UUID
    user_id: UUID
    display_name: str
    aliases: list[str] = Field(default_factory=list)
    relationship: str | None = None
    timezone: str | None = None
    important_dates: list[ContactImportantDate] = Field(default_factory=list)
    preferences: list[ContactPreference] = Field(default_factory=list)
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


def next_occurrence(today: date, month: int, day: int) -> date | None:
    """重要日期的下一次到来；2/29 在未来 5 年内无闰年时返回 None。"""
    for offset in range(5):
        year = today.year + offset
        days = calendar.monthrange(year, month)[1]
        if day > days:
            continue
        candidate = date(year, month, day)
        if candidate >= today:
            return candidate
    return None


def days_until(today: date, month: int, day: int) -> int | None:
    upcoming = next_occurrence(today, month, day)
    if upcoming is None:
        return None
    return (upcoming - today).days


__all__ = [
    "ContactImportantDate",
    "ContactPreference",
    "ContactView",
    "days_until",
    "next_occurrence",
]
