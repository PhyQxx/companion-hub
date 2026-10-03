"""CAL-01 日历存储：本地 CRUD 与重叠查询。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import CalendarEventRecord, Database
from app.harness.time import utc
from app.ids import uuid7
from app.tasks.store import TaskStore

from .models import CalendarEventView, CalendarParticipant


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _to_view(record: CalendarEventRecord) -> CalendarEventView:
    return CalendarEventView(
        id=record.id,
        user_id=record.user_id,
        calendar_id=record.calendar_id,
        title=record.title,
        notes=record.notes,
        starts_at=_aware(record.starts_at),
        ends_at=_aware(record.ends_at),
        all_day=record.all_day,
        location=record.location,
        participants=[CalendarParticipant.model_validate(item) for item in record.participants],
        status=record.status,
        source=record.source,
        created_at=_aware(record.created_at) if record.created_at is not None else None,
        updated_at=_aware(record.updated_at) if record.updated_at is not None else None,
    )


class CalendarStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        return self._database

    async def create_event(
        self,
        *,
        user_id: UUID,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        calendar_id: str = "primary",
        notes: str | None = None,
        location: str | None = None,
        all_day: bool = False,
        participants: list[CalendarParticipant] | None = None,
        source: str = "api",
        now: datetime | None = None,
    ) -> CalendarEventView:
        moment = now or datetime.now(UTC)
        if ends_at <= starts_at:
            raise ValueError("结束时间必须晚于开始时间")
        record = CalendarEventRecord(
            id=uuid7(),
            user_id=user_id,
            calendar_id=calendar_id,
            title=title,
            notes=notes,
            starts_at=starts_at,
            ends_at=ends_at,
            all_day=all_day,
            location=location,
            participants=[item.model_dump(mode="json") for item in participants or []],
            status="active",
            source=source,
            created_at=moment,
            updated_at=moment,
        )
        async with self._database.sessions.begin() as session:
            session.add(record)
        return _to_view(record)

    async def get_event(self, user_id: UUID, event_id: UUID) -> CalendarEventRecord:
        async with self._database.sessions() as session:
            record = await session.get(CalendarEventRecord, event_id)
        if record is None or record.user_id != user_id:
            raise LookupError("calendar event not found")
        return record

    async def list_events(
        self,
        user_id: UUID,
        *,
        starts_from: datetime | None = None,
        starts_to: datetime | None = None,
        include_cancelled: bool = False,
        limit: int = 200,
    ) -> list[CalendarEventView]:
        query = select(CalendarEventRecord).where(CalendarEventRecord.user_id == user_id)
        if not include_cancelled:
            query = query.where(CalendarEventRecord.status == "active")
        if starts_from is not None:
            query = query.where(CalendarEventRecord.starts_at >= starts_from)
        if starts_to is not None:
            query = query.where(CalendarEventRecord.starts_at < starts_to)
        query = query.order_by(CalendarEventRecord.starts_at).limit(limit)
        async with self._database.sessions() as session:
            records = (await session.execute(query)).scalars().all()
        return [_to_view(record) for record in records]

    async def overlapping(
        self,
        user_id: UUID,
        *,
        starts_at: datetime,
        ends_at: datetime,
        exclude_event_id: UUID | None = None,
    ) -> list[CalendarEventView]:
        """半开区间重叠（首尾相接不算冲突）。"""
        query = select(CalendarEventRecord).where(
            CalendarEventRecord.user_id == user_id,
            CalendarEventRecord.status == "active",
            CalendarEventRecord.starts_at < ends_at,
            CalendarEventRecord.ends_at > starts_at,
        )
        if exclude_event_id is not None:
            query = query.where(CalendarEventRecord.id != exclude_event_id)
        query = query.order_by(CalendarEventRecord.starts_at)
        async with self._database.sessions() as session:
            records = (await session.execute(query)).scalars().all()
        return [_to_view(record) for record in records]

    async def _lock_active(
        self, session: AsyncSession, user_id: UUID, event_id: UUID
    ) -> CalendarEventRecord:
        current = await session.scalar(
            update(CalendarEventRecord)
            .where(
                CalendarEventRecord.id == event_id,
                CalendarEventRecord.user_id == user_id,
                CalendarEventRecord.status == "active",
            )
            .values(updated_at=CalendarEventRecord.updated_at)
            .returning(CalendarEventRecord)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        if current is not None:
            return current
        existing = await session.scalar(
            select(CalendarEventRecord).where(
                CalendarEventRecord.id == event_id, CalendarEventRecord.user_id == user_id
            )
        )
        if existing is None:
            raise LookupError("calendar event not found")
        raise ValueError("only active events can be changed")

    async def update_event(
        self,
        user_id: UUID,
        event_id: UUID,
        *,
        title: str | None = None,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
        location: str | None = None,
        notes: str | None = None,
        now: datetime | None = None,
    ) -> CalendarEventView:
        moment = now or datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            managed = await self._lock_active(session, user_id, event_id)
            new_start, new_end = starts_at or managed.starts_at, ends_at or managed.ends_at
            if utc(new_end) <= utc(new_start):
                raise ValueError("结束时间必须晚于开始时间")
            if title is not None:
                managed.title = title
            if starts_at is not None:
                managed.starts_at = starts_at
            if ends_at is not None:
                managed.ends_at = ends_at
            if location is not None:
                managed.location = location
            if notes is not None:
                managed.notes = notes
            managed.updated_at = moment
            await TaskStore(self._database).cancel_tasks_by_source_ref_in_session(
                session, user_id, f"calendar:{event_id}", now=moment
            )
        return _to_view(managed)

    async def cancel_event(
        self,
        user_id: UUID,
        event_id: UUID,
        *,
        now: datetime | None = None,
    ) -> CalendarEventView:
        moment = now or datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            managed = await self._lock_active(session, user_id, event_id)
            managed.status = "cancelled"
            managed.updated_at = moment
            await TaskStore(self._database).cancel_tasks_by_source_ref_in_session(
                session, user_id, f"calendar:{event_id}", now=moment
            )
        return _to_view(managed)
