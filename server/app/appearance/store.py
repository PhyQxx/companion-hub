from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import select

from app.db import Database, UiPreferenceRecord, UiThemeRecord

ThemeSelection = Literal["pure-light", "midnight-violet", "system"]

PURE_LIGHT_ID = UUID("00000000-0000-7000-8000-000000000001")
MIDNIGHT_VIOLET_ID = UUID("00000000-0000-7000-8000-000000000002")

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


class ThemeStore:
    """受控主题定义与账户级外观偏好。v1 仅发布内置主题。"""

    def __init__(self, database: Database) -> None:
        self._database = database

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
            theme = await session.get(UiThemeRecord, preference.theme_id)
            if theme is None or theme.status != "published":
                theme = await session.get(UiThemeRecord, PURE_LIGHT_ID)
            if theme is None:
                raise RuntimeError("built-in themes are not initialized")
            selection_value = "system" if preference.appearance_mode == "system" else theme.key
            if selection_value not in {"pure-light", "midnight-violet", "system"}:
                selection_value = "pure-light"
            selection = cast(ThemeSelection, selection_value)
            return ThemePreferenceView(
                owner=owner,
                selection=selection,
                theme=self._theme_view(theme),
                appearance_mode=preference.appearance_mode,
                updated_at=preference.updated_at,
            )

    async def set_preference(
        self,
        selection: ThemeSelection,
        *,
        owner: str = "local-user",
    ) -> ThemePreferenceView:
        theme_id = MIDNIGHT_VIOLET_ID if selection == "midnight-violet" else PURE_LIGHT_ID
        appearance_mode = "system"
        if selection != "system":
            appearance_mode = "dark" if selection == "midnight-violet" else "light"
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            theme = await session.get(UiThemeRecord, theme_id)
            if theme is None or theme.status != "published":
                raise LookupError("theme not found")
            preference = await session.get(UiPreferenceRecord, owner)
            if preference is None:
                session.add(
                    UiPreferenceRecord(
                        owner=owner,
                        theme_id=theme_id,
                        appearance_mode=appearance_mode,
                        updated_at=now,
                    )
                )
            else:
                preference.theme_id = theme_id
                preference.appearance_mode = appearance_mode
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
