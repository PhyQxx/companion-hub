from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db import Database, UiPreferenceRecord, UiThemeRecord

ThemeSelection = Literal["pure-light", "midnight-violet", "system", "scheduled"]

PURE_LIGHT_ID = UUID("00000000-0000-7000-8000-000000000001")
MIDNIGHT_VIOLET_ID = UUID("00000000-0000-7000-8000-000000000002")

# 定时主题缺省边界：白天 07:00 起用纯净明亮，19:00 起用静夜紫
DEFAULT_LIGHT_TIME = "07:00"
DEFAULT_DARK_TIME = "19:00"
_HHMM_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

BUILTIN_THEMES: tuple[dict[str, Any], ...] = (
    {
        "id": PURE_LIGHT_ID,
        "key": "pure-light",
        "name": "纯净明亮",
        "mode": "light",
        "content_hash": "builtin-pure-light-v1",
        "definition": {
            "description": "与 Aria 管理后台一致的高可读性浅色主题。",
            "swatches": ["#f6f8fc", "#ffffff", "#4f6df5", "#172033"],
            "tokens": {
                "color.bg.canvas": "#f6f8fc",
                "color.bg.surface": "#ffffff",
                "color.text.primary": "#172033",
                "color.text.secondary": "#68748a",
                "color.border.default": "#e3e8f2",
                "color.brand.primary": "#4f6df5",
            },
        },
    },
    {
        "id": MIDNIGHT_VIOLET_ID,
        "key": "midnight-violet",
        "name": "静夜紫",
        "mode": "dark",
        "content_hash": "builtin-midnight-violet-v1",
        "definition": {
            "description": "深色、安静、轻未来感; 适合夜间陪伴。",
            "swatches": ["#0c0e15", "#141824", "#7c8ff5", "#e7eaf3"],
            "tokens": {
                "color.bg.canvas": "#0c0e15",
                "color.bg.surface": "#141824",
                "color.text.primary": "#e7eaf3",
                "color.text.secondary": "#929caf",
                "color.border.default": "#2a3041",
                "color.brand.primary": "#7c8ff5",
            },
        },
    },
)


@dataclass(frozen=True, slots=True)
class ThemeView:
    id: UUID
    key: str
    name: str
    mode: str
    schema_version: int
    version: int
    definition: dict[str, Any]
    content_hash: str
    built_in: bool
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ThemePreferenceView:
    owner: str
    selection: ThemeSelection
    theme: ThemeView
    appearance_mode: str
    updated_at: datetime | None
    # 定时主题的切换边界（light_time 起用明亮、dark_time 起用深色）；非定时模式为 None
    schedule: tuple[str, str] | None = None


def validate_schedule(light_time: str, dark_time: str) -> tuple[str, str]:
    """校验 HH:MM 边界；两个时刻必须可区分（相等无法形成切换窗口）。"""
    for value in (light_time, dark_time):
        if not _HHMM_PATTERN.match(value):
            raise ValueError(f"定时边界须为 HH:MM：{value}")
    if light_time == dark_time:
        raise ValueError("定时主题的明暗切换时刻不能相同")
    return light_time, dark_time


def schedule_prefers_light(light_time: str, dark_time: str, now: datetime) -> bool:
    """按本地时钟判断当前应使用明亮主题；支持亮窗跨夜的倒置配置。"""
    minute = now.hour * 60 + now.minute

    def to_minutes(value: str) -> int:
        hour, _, minute_part = value.partition(":")
        return int(hour) * 60 + int(minute_part)

    light = to_minutes(light_time)
    dark = to_minutes(dark_time)
    if light < dark:
        return light <= minute < dark
    return not (dark <= minute < light)


class ThemeStore:
    """受控主题定义与账户级外观偏好。v1 仅发布内置主题。"""

    def __init__(self, database: Database, *, timezone_name: str | None = None) -> None:
        self._database = database
        resolved_timezone = timezone_name or os.getenv("ARIA_DEFAULT_TIMEZONE") or "Asia/Shanghai"
        self._tz = ZoneInfo(resolved_timezone)

    async def load_builtin_themes(self) -> int:
        added = 0
        async with self._database.sessions.begin() as session:
            for item in BUILTIN_THEMES:
                record = await session.get(UiThemeRecord, item["id"])
                if record is None:
                    session.add(
                        UiThemeRecord(
                            id=item["id"],
                            key=item["key"],
                            name=item["name"],
                            mode=item["mode"],
                            schema_version=1,
                            version=1,
                            status="published",
                            definition=item["definition"],
                            content_hash=item["content_hash"],
                            built_in=True,
                        )
                    )
                    added += 1
                elif record.content_hash != item["content_hash"]:
                    record.name = item["name"]
                    record.mode = item["mode"]
                    record.definition = item["definition"]
                    record.content_hash = item["content_hash"]
                    record.version += 1
                    record.status = "published"
                    record.built_in = True
        return added

    async def list_themes(self) -> list[ThemeView]:
        async with self._database.sessions() as session:
            records = (
                await session.scalars(
                    select(UiThemeRecord)
                    .where(UiThemeRecord.status == "published")
                    .order_by(UiThemeRecord.built_in.desc(), UiThemeRecord.name)
                )
            ).all()
        return [self._theme_view(record) for record in records]

    async def get_preference(self, owner: str = "local-user") -> ThemePreferenceView:
        async with self._database.sessions() as session:
            preference = await session.get(UiPreferenceRecord, owner)
            if preference is None:
                theme = await session.get(UiThemeRecord, PURE_LIGHT_ID)
                if theme is None:
                    raise RuntimeError("built-in themes are not initialized")
                return ThemePreferenceView(
                    owner=owner,
                    selection="pure-light",
                    theme=self._theme_view(theme),
                    appearance_mode="light",
                    updated_at=None,
                )
            schedule: tuple[str, str] | None = None
            if preference.appearance_mode == "scheduled":
                schedule = validate_schedule(
                    preference.schedule_light_time or DEFAULT_LIGHT_TIME,
                    preference.schedule_dark_time or DEFAULT_DARK_TIME,
                )
                assert schedule is not None
                theme_id = (
                    PURE_LIGHT_ID
                    if schedule_prefers_light(
                        schedule[0], schedule[1], datetime.now(UTC).astimezone(self._tz)
                    )
                    else MIDNIGHT_VIOLET_ID
                )
            else:
                theme_id = preference.theme_id
            theme = await session.get(UiThemeRecord, theme_id)
            if theme is None or theme.status != "published":
                theme = await session.get(UiThemeRecord, PURE_LIGHT_ID)
            if theme is None:
                raise RuntimeError("built-in themes are not initialized")
            selection_value = "system" if preference.appearance_mode == "system" else (
                "scheduled" if preference.appearance_mode == "scheduled" else theme.key
            )
            if selection_value not in {"pure-light", "midnight-violet", "system", "scheduled"}:
                selection_value = "pure-light"
            selection = cast(ThemeSelection, selection_value)
            return ThemePreferenceView(
                owner=owner,
                selection=selection,
                theme=self._theme_view(theme),
                appearance_mode=preference.appearance_mode,
                updated_at=preference.updated_at,
                schedule=schedule,
            )

    async def set_preference(
        self,
        selection: ThemeSelection,
        *,
        owner: str = "local-user",
        light_time: str | None = None,
        dark_time: str | None = None,
    ) -> ThemePreferenceView:
        schedule = (
            validate_schedule(light_time or DEFAULT_LIGHT_TIME, dark_time or DEFAULT_DARK_TIME)
            if selection == "scheduled"
            else None
        )
        theme_id = MIDNIGHT_VIOLET_ID if selection == "midnight-violet" else PURE_LIGHT_ID
        appearance_mode = "system"
        if selection != "system":
            appearance_mode = (
                "dark"
                if selection == "midnight-violet"
                else "light" if selection == "pure-light" else "scheduled"
            )
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            theme = await session.get(UiThemeRecord, theme_id)
            if theme is None or theme.status != "published":
                raise LookupError("theme not found")
            preference = await session.get(UiPreferenceRecord, owner)
            schedule_values: dict[str, str | None] = {
                "schedule_light_time": schedule[0] if schedule else None,
                "schedule_dark_time": schedule[1] if schedule else None,
            }
            if preference is None:
                session.add(
                    UiPreferenceRecord(
                        owner=owner,
                        theme_id=theme_id,
                        appearance_mode=appearance_mode,
                        updated_at=now,
                        **schedule_values,
                    )
                )
            else:
                preference.theme_id = theme_id
                preference.appearance_mode = appearance_mode
                preference.schedule_light_time = schedule_values["schedule_light_time"]
                preference.schedule_dark_time = schedule_values["schedule_dark_time"]
                preference.updated_at = now
        return await self.get_preference(owner)

    @staticmethod
    def _theme_view(record: UiThemeRecord) -> ThemeView:
        return ThemeView(
            id=record.id,
            key=record.key,
            name=record.name,
            mode=record.mode,
            schema_version=record.schema_version,
            version=record.version,
            definition=record.definition,
            content_hash=record.content_hash,
            built_in=record.built_in,
            updated_at=record.updated_at,
        )
