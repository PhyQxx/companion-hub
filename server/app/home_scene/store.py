"""HOME-01 场景存储：CRUD 与用户内名称唯一。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import Database, HomeSceneRecord
from app.ids import uuid7

from .models import HomeSceneStep, HomeSceneView, normalize_window


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _to_view(record: HomeSceneRecord) -> HomeSceneView:
    return HomeSceneView(
        id=record.id,
        user_id=record.user_id,
        name=record.name,
        trigger=record.trigger,
        window_start=record.window_start,
        window_end=record.window_end,
        steps=[HomeSceneStep.model_validate(item) for item in record.steps or []],
        enabled=record.enabled,
        created_at=_aware(record.created_at),
        updated_at=_aware(record.updated_at),
    )


def _clean_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned or len(cleaned) > 120:
        raise ValueError("场景名称必须为 1-120 字符")
    return cleaned


class HomeSceneStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def create_scene(
        self,
        *,
        user_id: UUID,
        name: str,
        trigger: str,
        steps: list[HomeSceneStep],
        window_start: str | None = None,
        window_end: str | None = None,
        now: datetime | None = None,
    ) -> HomeSceneView:
        cleaned_name = _clean_name(name)
        cleaned_start = normalize_window(window_start) if window_start else None
        cleaned_end = normalize_window(window_end) if window_end else None
        moment = now or datetime.now(UTC)
        record = HomeSceneRecord(
            id=uuid7(),
            user_id=user_id,
            name=cleaned_name,
            trigger=trigger.strip(),
            window_start=cleaned_start,
            window_end=cleaned_end,
            steps=[step.model_dump(mode="json") for step in steps],
            enabled=True,
            created_at=moment,
            updated_at=moment,
        )
        try:
            async with self._database.sessions.begin() as session:
                session.add(record)
        except IntegrityError as error:
            raise ValueError(f"同名场景已存在：{cleaned_name}") from error
        return _to_view(record)

    async def get_scene(self, user_id: UUID, scene_id: UUID) -> HomeSceneRecord:
        async with self._database.sessions() as session:
            record = await session.get(HomeSceneRecord, scene_id)
        if record is None or record.user_id != user_id:
            raise LookupError("home scene not found")
        return record

    async def get_scene_view(self, user_id: UUID, scene_id: UUID) -> HomeSceneView:
        return _to_view(await self.get_scene(user_id, scene_id))

    async def list_scenes(self, user_id: UUID, *, limit: int = 100) -> list[HomeSceneView]:
        query = (
            select(HomeSceneRecord)
            .where(HomeSceneRecord.user_id == user_id)
            .order_by(HomeSceneRecord.name)
            .limit(limit)
        )
        async with self._database.sessions() as session:
            records = (await session.execute(query)).scalars().all()
        return [_to_view(record) for record in records]

    async def enabled_scenes_for_trigger(
        self, user_id: UUID, trigger: str
    ) -> list[HomeSceneView]:
        query = select(HomeSceneRecord).where(
            HomeSceneRecord.user_id == user_id,
            HomeSceneRecord.trigger == trigger,
            HomeSceneRecord.enabled.is_(True),
        )
        async with self._database.sessions() as session:
            records = (await session.execute(query)).scalars().all()
        return [_to_view(record) for record in records]

    async def set_enabled(self, user_id: UUID, scene_id: UUID, *, enabled: bool) -> HomeSceneView:
        record = await self.get_scene(user_id, scene_id)
        moment = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            managed = await session.get(HomeSceneRecord, scene_id)
            assert managed is not None
            managed.enabled = enabled
            managed.updated_at = moment
        return await self.get_scene_view(user_id, record.id)

    async def delete_scene(self, user_id: UUID, scene_id: UUID) -> None:
        await self.get_scene(user_id, scene_id)
        async with self._database.sessions.begin() as session:
            managed = await session.get(HomeSceneRecord, scene_id)
            assert managed is not None
            await session.delete(managed)
