"""CAL-01 外部日历镜像引擎：CalDAV / Google 共用的 upsert 与删除联动。

镜像语义（与 TODO-01 pnkx 镜像同构，只读不回写）：
- 按 source_ref upsert，etag 未变跳过；
- 远端取消/删除/窗口滑出 → 本地镜像取消；
- 镜像不创建本地提醒任务（外部日历自带通知）；
- 本地（source 不是外部源）事件永不触碰。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update

from app.db import CalendarEventRecord, Database
from app.ids import uuid7

logger = logging.getLogger("app.calendar.mirror")


class MirrorOccurrence:
    """一次具体出现的外部事件投影（周期事件已按次展开）。"""

    __slots__ = (
        "all_day",
        "cancelled",
        "description",
        "ends_at",
        "etag",
        "location",
        "ref",
        "starts_at",
        "summary",
    )

    def __init__(
        self,
        *,
        ref: str,
        summary: str,
        starts_at: datetime,
        ends_at: datetime,
        all_day: bool = False,
        location: str | None = None,
        description: str | None = None,
        cancelled: bool = False,
        etag: str = "",
    ) -> None:
        self.ref = ref
        self.summary = summary
        self.starts_at = starts_at
        self.ends_at = ends_at
        self.all_day = all_day
        self.location = location
        self.description = description
        self.cancelled = cancelled
        self.etag = etag


class CalendarMirrorStats:
    __slots__ = ("mirrors_cancelled", "mirrors_created", "mirrors_updated")

    def __init__(self) -> None:
        self.mirrors_created = 0
        self.mirrors_updated = 0
        self.mirrors_cancelled = 0

    def merge_into(self, target: Any) -> None:
        """把本计数并入同步服务的 SyncStats（字段名一致）。"""
        target.mirrors_created += self.mirrors_created
        target.mirrors_updated += self.mirrors_updated
        target.mirrors_cancelled += self.mirrors_cancelled


class CalendarMirrorService:
    def __init__(self, database: Database, *, source: str) -> None:
        self._database = database
        self._source = source

    async def local_mirrors(self, user_id: UUID) -> dict[str, CalendarEventRecord]:
        async with self._database.sessions() as session:
            records = (
                await session.scalars(
                    select(CalendarEventRecord)
                    .where(
                        CalendarEventRecord.user_id == user_id,
                        CalendarEventRecord.source == self._source,
                        CalendarEventRecord.source_ref.is_not(None),
                    )
                    .limit(2000)
                )
            ).all()
        return {record.source_ref: record for record in records if record.source_ref}

    async def apply(
        self,
        user_id: UUID,
        calendar_id: str,
        occurrences_by_ref: dict[str, MirrorOccurrence],
        *,
        stats: Any,
        now: datetime | None = None,
    ) -> None:
        local_by_ref = await self.local_mirrors(user_id)
        moment = now or datetime.now(UTC)
        counter = CalendarMirrorStats()
        for ref, occurrence in occurrences_by_ref.items():
            local = local_by_ref.get(ref)
            if occurrence.cancelled:
                if local is not None and local.status == "active":
                    await self._set_status(local.id, "cancelled", moment)
                    counter.mirrors_cancelled += 1
                continue
            if local is not None and local.external_etag == occurrence.etag:
                continue
            if local is None:
                async with self._database.sessions.begin() as session:
                    session.add(
                        _mirror_record(
                            user_id, self._source, calendar_id, occurrence, moment
                        )
                    )
                counter.mirrors_created += 1
            else:
                async with self._database.sessions.begin() as session:
                    await session.execute(
                        update(CalendarEventRecord)
                        .where(CalendarEventRecord.id == local.id)
                        .values(
                            title=occurrence.summary,
                            starts_at=occurrence.starts_at,
                            ends_at=occurrence.ends_at,
                            all_day=occurrence.all_day,
                            location=occurrence.location,
                            notes=occurrence.description,
                            status="active",
                            calendar_id=calendar_id,
                            external_etag=occurrence.etag or None,
                            updated_at=moment,
                        )
                    )
                counter.mirrors_updated += 1
        # 远端删除 / 窗口滑出的镜像取消
        for ref, local in local_by_ref.items():
            if ref in occurrences_by_ref:
                continue
            if local.status == "active":
                await self._set_status(local.id, "cancelled", moment)
                counter.mirrors_cancelled += 1
        counter.merge_into(stats)

    async def _set_status(self, event_id: UUID, status: str, now: datetime) -> None:
        async with self._database.sessions.begin() as session:
            await session.execute(
                update(CalendarEventRecord)
                .where(CalendarEventRecord.id == event_id)
                .values(status=status, updated_at=now)
            )


def _mirror_record(
    user_id: UUID,
    source: str,
    calendar_id: str,
    occurrence: MirrorOccurrence,
    now: datetime,
) -> CalendarEventRecord:
    return CalendarEventRecord(
        id=uuid7(),
        user_id=user_id,
        calendar_id=calendar_id[:64],
        title=occurrence.summary,
        notes=occurrence.description,
        starts_at=occurrence.starts_at,
        ends_at=occurrence.ends_at,
        all_day=occurrence.all_day,
        location=occurrence.location,
        participants=[],
        status="cancelled" if occurrence.cancelled else "active",
        source=source,
        source_ref=occurrence.ref[:160],
        external_etag=(occurrence.etag[:128] if occurrence.etag else None),
        created_at=now,
        updated_at=now,
    )


__all__ = ["CalendarMirrorService", "MirrorOccurrence"]
