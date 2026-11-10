"""Bounded, deterministic daily/weekly reports from owned L0/L1 facts."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError

from app.db import (
    ActivitySummaryRecord,
    AppUserRecord,
    CalendarEventRecord,
    Database,
    TaskItemRecord,
    TimelineEventRecord,
)
from app.ids import uuid7
from app.schemas.common import StrictModel

Kind = Literal["daily", "weekly"]


class ActivitySummaryView(StrictModel):
    id: UUID
    kind: Kind
    period_start: date
    period_end: date
    timezone: str
    text: str
    created_at: datetime


def view(row: ActivitySummaryRecord) -> ActivitySummaryView:
    return ActivitySummaryView.model_validate(row, from_attributes=True)


class ActivitySummaryService:
    def __init__(self, database: Database, *, timezone_name: str = "Asia/Shanghai") -> None:
        self.database = database
        self.tz = ZoneInfo(timezone_name)

    async def active_user_ids(self) -> list[UUID]:
        async with self.database.sessions() as session:
            return list(
                await session.scalars(
                    select(AppUserRecord.id).where(AppUserRecord.status == "active")
                )
            )

    async def recent(
        self, user_id: UUID, *, kind: Kind | None = None, limit: int = 30, offset: int = 0
    ) -> list[ActivitySummaryView]:
        statement = select(ActivitySummaryRecord).where(ActivitySummaryRecord.user_id == user_id)
        if kind is not None:
            statement = statement.where(ActivitySummaryRecord.kind == kind)
        async with self.database.sessions() as session:
            rows = await session.scalars(
                statement.order_by(
                    ActivitySummaryRecord.period_start.desc(), ActivitySummaryRecord.kind
                )
                .limit(limit)
                .offset(offset)
            )
            return [view(row) for row in rows]

    async def remove(self, user_id: UUID, summary_id: UUID) -> None:
        async with self.database.sessions.begin() as session:
            await session.execute(
                delete(ActivitySummaryRecord).where(
                    ActivitySummaryRecord.user_id == user_id, ActivitySummaryRecord.id == summary_id
                )
            )

    async def build(
        self, user_id: UUID, kind: Kind, *, now: datetime | None = None
    ) -> ActivitySummaryView:
        moment = now or datetime.now(UTC)
        today = moment.astimezone(self.tz).date()
        end = today if kind == "daily" else today - timedelta(days=today.weekday())
        start = end - timedelta(days=1 if kind == "daily" else 7)
        begin = datetime.combine(start, time.min, self.tz).astimezone(UTC)
        finish = datetime.combine(end, time.min, self.tz).astimezone(UTC)
        key = select(ActivitySummaryRecord).where(
            ActivitySummaryRecord.user_id == user_id,
            ActivitySummaryRecord.kind == kind,
            ActivitySummaryRecord.period_start == start,
        )
        async with self.database.sessions() as session:
            existing = await session.scalar(key)
            if existing is not None:
                return view(existing)
            lines = [
                f"{'每日' if kind == 'daily' else '每周'}综合总结 · "
                f"{start} 至 {end - timedelta(days=1)}",
                "仅含已记录的 L0/L1 事实；各区块最多展示 100 条。",
            ]
            for title, predicate in [
                ("聊天", TimelineEventRecord.source_type == "message"),
                ("浏览", TimelineEventRecord.event_type.like("browser.%")),
                ("屏幕", TimelineEventRecord.event_type.like("screen.%")),
            ]:
                rows = list(
                    await session.scalars(
                        select(TimelineEventRecord)
                        .where(
                            TimelineEventRecord.user_id == user_id,
                            TimelineEventRecord.privacy_level.in_(["L0", "L1"]),
                            TimelineEventRecord.occurred_at >= begin,
                            TimelineEventRecord.occurred_at < finish,
                            predicate,
                        )
                        .order_by(TimelineEventRecord.occurred_at, TimelineEventRecord.id)
                        .limit(101)
                    )
                )
                lines.append(f"\n【{title}】")
                lines.extend(f"- {r.summary[:400]} [timeline:{r.id}]" for r in rows[:100])
                if not rows:
                    lines.append("无记录。")
                if len(rows) > 100:
                    lines.append("记录较多，仅展示前 100 条。")
            tasks = list(
                await session.scalars(
                    select(TaskItemRecord)
                    .where(
                        TaskItemRecord.user_id == user_id,
                        TaskItemRecord.privacy_level.in_(["L0", "L1"]),
                        or_(
                            (TaskItemRecord.created_at >= begin)
                            & (TaskItemRecord.created_at < finish),
                            (TaskItemRecord.completed_at >= begin)
                            & (TaskItemRecord.completed_at < finish),
                        ),
                    )
                    .order_by(TaskItemRecord.created_at, TaskItemRecord.id)
                    .limit(101)
                )
            )
            events = list(
                await session.scalars(
                    select(CalendarEventRecord)
                    .where(
                        CalendarEventRecord.user_id == user_id,
                        CalendarEventRecord.status == "active",
                        CalendarEventRecord.starts_at < finish,
                        CalendarEventRecord.ends_at > begin,
                    )
                    .order_by(CalendarEventRecord.starts_at, CalendarEventRecord.id)
                    .limit(101)
                )
            )
            for title, entries in [
                (
                    "任务（期间新建或完成；状态为生成时状态）",
                    [f"{r.title[:400]} · {r.status} [task:{r.id}]" for r in tasks],
                ),
                (
                    "日程（计划安排，不表示已参加）",
                    [f"{r.title[:400]} [calendar:{r.id}]" for r in events],
                ),
            ]:
                lines.append(f"\n【{title}】")
                lines.extend(f"- {entry}" for entry in entries[:100])
                if not entries:
                    lines.append("无记录。")
                if len(entries) > 100:
                    lines.append("记录较多，仅展示前 100 条。")
        row = ActivitySummaryRecord(
            id=uuid7(),
            user_id=user_id,
            kind=kind,
            period_start=start,
            period_end=end,
            timezone=str(self.tz),
            text="\n".join(lines),
            created_at=moment,
        )
        try:
            async with self.database.sessions.begin() as session:
                session.add(row)
        except IntegrityError:
            async with self.database.sessions() as session:
                existing = await session.scalar(key)
                if existing is None:
                    raise
                return view(existing)
        return view(row)
