"""CAL-01 外部日历镜像引擎：CalDAV / Google 共用的 upsert 与删除联动。

镜像语义（与 TODO-01 pnkx 镜像同构，只读不回写）：
- 按 source_ref upsert，etag 未变跳过；
- 远端取消/删除/窗口滑出 → 本地镜像取消；
- 镜像不创建本地提醒任务（外部日历自带通知）；
- 本地（source 不是外部源）事件永不触碰。
"""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AppUserRecord, CalendarEventRecord, Database
from app.harness.time import utc
from app.ids import uuid7
from app.tasks.store import TaskStore

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

    async def _local_rows(
        self, session: AsyncSession, user_id: UUID, *, lock: bool = False
    ) -> list[CalendarEventRecord]:
        records: list[CalendarEventRecord] = []
        cursor: UUID | None = None
        while True:
            query = select(CalendarEventRecord).where(
                CalendarEventRecord.user_id == user_id,
                CalendarEventRecord.source == self._source,
                CalendarEventRecord.source_ref.is_not(None),
            )
            if cursor is not None:
                query = query.where(CalendarEventRecord.id > cursor)
            query = query.order_by(CalendarEventRecord.id).limit(500)
            if lock:
                query = query.with_for_update().execution_options(populate_existing=True)
            page = list(await session.scalars(query))
            records.extend(page)
            if len(page) < 500:
                return records
            cursor = page[-1].id

    async def local_mirrors(self, user_id: UUID) -> dict[str, CalendarEventRecord]:
        async with self._database.sessions() as session:
            records = await self._local_rows(session, user_id)
        return _canonical_records(records)[0]

    def _cancel_local(
        self,
        local: CalendarEventRecord,
        counter: CalendarMirrorStats,
        moment: datetime,
    ) -> str:
        if local.status == "active":
            local.status = "cancelled"
            local.updated_at = moment
            counter.mirrors_cancelled += 1
        return f"calendar:{local.id}"

    async def apply(
        self,
        user_id: UUID,
        calendar_id: str,
        occurrences_by_ref: dict[str, MirrorOccurrence],
        *,
        stats: Any,
        now: datetime | None = None,
    ) -> None:
        occurrences_by_ref = deepcopy(occurrences_by_ref)
        if any(
            ref != occurrence.ref or not ref or len(ref) > 160
            for ref, occurrence in occurrences_by_ref.items()
        ):
            raise ValueError("calendar_mirror_reference_invalid")
        calendar_id = calendar_id[:64]
        moment = now or datetime.now(UTC)
        counter = CalendarMirrorStats()
        async with self._database.sessions.begin() as session:
            owner = await session.scalar(
                update(AppUserRecord)
                .where(AppUserRecord.id == user_id, AppUserRecord.status == "active")
                .values(status=AppUserRecord.status)
                .returning(AppUserRecord.id)
            )
            if owner is None:
                raise PermissionError("calendar_mirror_owner_inactive")
            records = await self._local_rows(session, user_id, lock=True)
            local_by_ref, duplicates = _canonical_records(records)
            cleanup_refs: set[str] = set()
            for duplicate in duplicates:
                cleanup_refs.add(self._cancel_local(duplicate, counter, moment))
            for ref, occurrence in sorted(occurrences_by_ref.items()):
                local = local_by_ref.get(ref)
                if occurrence.cancelled:
                    if local is not None:
                        cleanup_refs.add(self._cancel_local(local, counter, moment))
                    continue
                if local is None:
                    session.add(
                        _mirror_record(user_id, self._source, calendar_id, occurrence, moment)
                    )
                    counter.mirrors_created += 1
                    continue
                if _same_occurrence(local, occurrence, calendar_id):
                    continue
                local.title = occurrence.summary
                local.starts_at = occurrence.starts_at
                local.ends_at = occurrence.ends_at
                local.all_day = occurrence.all_day
                local.location = occurrence.location
                local.notes = occurrence.description
                local.status = "active"
                local.calendar_id = calendar_id
                local.external_etag = occurrence.etag[:128] or None
                local.updated_at = moment
                cleanup_refs.add(f"calendar:{local.id}")
                counter.mirrors_updated += 1
            for ref, local in local_by_ref.items():
                if ref not in occurrences_by_ref:
                    cleanup_refs.add(self._cancel_local(local, counter, moment))
            if cleanup_refs:
                await TaskStore(self._database).cancel_tasks_by_source_refs_in_session(
                    session, user_id, sorted(cleanup_refs), now=moment
                )
        counter.merge_into(stats)


def _canonical_records(
    records: list[CalendarEventRecord],
) -> tuple[dict[str, CalendarEventRecord], list[CalendarEventRecord]]:
    canonical: dict[str, CalendarEventRecord] = {}
    duplicates: list[CalendarEventRecord] = []
    for record in records:
        if record.source_ref is None:
            continue
        existing = canonical.get(record.source_ref)
        if existing is None:
            canonical[record.source_ref] = record
        elif (utc(record.updated_at), record.id) > (utc(existing.updated_at), existing.id):
            duplicates.append(existing)
            canonical[record.source_ref] = record
        else:
            duplicates.append(record)
    return canonical, sorted(duplicates, key=lambda record: record.id)


def _same_occurrence(
    local: CalendarEventRecord, occurrence: MirrorOccurrence, calendar_id: str
) -> bool:
    return (
        local.status == "active"
        and local.calendar_id == calendar_id
        and local.title == occurrence.summary
        and utc(local.starts_at) == utc(occurrence.starts_at)
        and utc(local.ends_at) == utc(occurrence.ends_at)
        and local.all_day == occurrence.all_day
        and local.location == occurrence.location
        and local.notes == occurrence.description
        and local.external_etag == (occurrence.etag[:128] or None)
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
