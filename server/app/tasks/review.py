"""REVIEW-01 晚间回顾：四区块确定性采集、逐项修正、每晚一次投递。

区块：完成事项（当日完成的任务/承诺）、未完成计划（到期未完成的任务/
承诺）、新承诺（当日新建）、明日重点（明天触发的任务/到期承诺）。

边界（对应验收）：
- 用户可逐项修正：confirm / remove / 附注 note，修正只落在回顾自身；
- 不直接改写 Persona、Memory 或任务/目标状态——回顾是只读事实 + 修正
  记录，任何沉淀都必须走既有确认链路。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.cognition.store import CognitiveStore
from app.db import AppUserRecord, DailyReviewRecord, Database
from app.ids import uuid7
from app.schemas.common import PrivacyLevel, StrictModel
from app.tasks.models import TaskStatus
from app.tasks.store import TaskStore

TRIGGER_KIND_REVIEW = "review.evening"
MAX_ITEMS_PER_SECTION = 5
MAX_TEXT_CHARS = 1_600

ReviewSection = Literal["completed", "unfinished", "new_commitment", "tomorrow"]


class ReviewItem(StrictModel):
    section: ReviewSection
    text: Annotated[str, Field(min_length=1, max_length=280)]
    source: Annotated[str, Field(min_length=1, max_length=120)]
    action: Literal["pending", "confirmed", "removed"] = "pending"
    note: Annotated[str, Field(min_length=1, max_length=200)] | None = None


class ReviewView(StrictModel):
    id: UUID
    user_id: UUID
    review_date: date
    items: list[ReviewItem]
    text: str
    status: Annotated[str, Field(pattern="^(pending|delivered)$")]
    delivered_at: datetime | None = None
    channels: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


ReviewDeliverer = Callable[..., Awaitable[list[str] | None]]

_SECTION_TITLES: dict[str, str] = {
    "completed": "完成事项",
    "unfinished": "未完成计划",
    "new_commitment": "新承诺",
    "tomorrow": "明日重点",
}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _to_view(record: DailyReviewRecord) -> ReviewView:
    return ReviewView(
        id=record.id,
        user_id=record.user_id,
        review_date=record.review_date,
        items=[ReviewItem.model_validate(item) for item in record.items],
        text=record.text,
        status=record.status,
        delivered_at=_aware(record.delivered_at),
        channels=[str(item) for item in record.channels or []],
        created_at=_aware(record.created_at),
    )


class DailyReviewService:
    def __init__(
        self,
        database: Database,
        task_store: TaskStore,
        cognitive_store: CognitiveStore,
        *,
        calendar_store: Any | None = None,
        timezone_name: str = "Asia/Shanghai",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._tasks = task_store
        self._goals = cognitive_store
        self._calendar = calendar_store
        self._tz = ZoneInfo(timezone_name)
        self._clock = clock or (lambda: datetime.now(UTC))

    async def active_user_ids(self) -> list[UUID]:
        async with self._database.sessions() as session:
            rows = await session.scalars(
                select(AppUserRecord.id)
                .where(AppUserRecord.status == "active")
                .order_by(AppUserRecord.created_at)
            )
        return list(rows)

    async def get_review(self, user_id: UUID, review_date: date) -> ReviewView | None:
        async with self._database.sessions() as session:
            record = await session.scalar(
                select(DailyReviewRecord).where(
                    DailyReviewRecord.user_id == user_id,
                    DailyReviewRecord.review_date == review_date,
                )
            )
        return _to_view(record) if record is not None else None

    async def latest_review(self, user_id: UUID) -> ReviewView | None:
        async with self._database.sessions() as session:
            record = await session.scalar(
                select(DailyReviewRecord)
                .where(DailyReviewRecord.user_id == user_id)
                .order_by(DailyReviewRecord.review_date.desc())
                .limit(1)
            )
        return _to_view(record) if record is not None else None

    async def recent_reviews(self, user_id: UUID, *, limit: int = 30) -> list[ReviewView]:
        async with self._database.sessions() as session:
            records = await session.scalars(
                select(DailyReviewRecord)
                .where(DailyReviewRecord.user_id == user_id)
                .order_by(DailyReviewRecord.review_date.desc())
                .limit(limit)
            )
        return [_to_view(record) for record in records]

    async def build(self, user_id: UUID, *, review_date: date | None = None) -> ReviewView:
        """采集当日事实并落库；同日已存在直接返回（幂等，不覆盖修正）。"""
        day = review_date or self._clock().astimezone(self._tz).date()
        existing = await self.get_review(user_id, day)
        if existing is not None:
            return existing
        items = await self.collect_items(user_id, review_date=day)
        record = DailyReviewRecord(
            id=uuid7(),
            user_id=user_id,
            review_date=day,
            items=[item.model_dump(mode="json") for item in items],
            text=compose_review_text(day, items),
            status="pending",
            created_at=self._clock(),
            updated_at=self._clock(),
        )
        try:
            async with self._database.sessions.begin() as session:
                session.add(record)
        except IntegrityError:
            existing = await self.get_review(user_id, day)
            assert existing is not None
            return existing
        return _to_view(record)

    async def collect_items(self, user_id: UUID, *, review_date: date) -> list[ReviewItem]:
        day_start = datetime.combine(review_date, dt_time.min, tzinfo=self._tz)
        day_end = day_start + timedelta(days=1)
        tomorrow_end = day_end + timedelta(days=1)
        items: list[ReviewItem] = []

        for task in await self._tasks.list_tasks(user_id, status=TaskStatus.DONE, limit=200):
            completed = _aware(task.completed_at)
            if completed is None or not day_start <= completed < day_end:
                continue
            items.append(
                ReviewItem(
                    section="completed",
                    text=task.title,
                    source=f"task:{task.id}",
                )
            )
        for goal in await self._goals.goals_completed_between(
            user_id, start=day_start, end=day_end
        ):
            items.append(ReviewItem(section="completed", text=goal.title, source=f"goal:{goal.id}"))

        for task in await self._tasks.list_tasks(user_id, status=TaskStatus.ACTIVE, limit=200):
            next_fire = _aware(task.next_fire_at)
            if task.trigger.type != "time" or next_fire is None or next_fire >= day_end:
                continue
            overdue = (
                "已逾期"
                if next_fire < day_start
                else next_fire.astimezone(self._tz).strftime("%H:%M")
            )
            items.append(
                ReviewItem(
                    section="unfinished",
                    text=f"[{overdue}] {task.title}",
                    source=f"task:{task.id}",
                )
            )
        for goal in await self._goals.active_goals(user_id, now=self._clock()):
            due = _aware(goal.due_at)
            if due is None or due >= day_end:
                continue
            items.append(
                ReviewItem(section="unfinished", text=goal.title, source=f"goal:{goal.id}")
            )

        for goal in await self._goals.goals_created_between(user_id, start=day_start, end=day_end):
            items.append(
                ReviewItem(section="new_commitment", text=goal.title, source=f"goal:{goal.id}")
            )

        for task in await self._tasks.list_tasks(user_id, status=TaskStatus.ACTIVE, limit=200):
            next_fire = _aware(task.next_fire_at)
            if task.trigger.type != "time" or next_fire is None:
                continue
            if not day_end <= next_fire < tomorrow_end:
                continue
            items.append(
                ReviewItem(
                    section="tomorrow",
                    text=f"{next_fire.astimezone(self._tz).strftime('%H:%M')} {task.title}",
                    source=f"task:{task.id}",
                )
            )
        for goal in await self._goals.active_goals(user_id, now=self._clock()):
            due = _aware(goal.due_at)
            if due is None or not day_end <= due < tomorrow_end:
                continue
            items.append(
                ReviewItem(
                    section="tomorrow",
                    text=goal.title,
                    source=f"goal:{goal.id}",
                )
            )

        # 明日日程（本地 + 外部镜像；全天日程只列标题）
        if self._calendar is not None:
            try:
                # 与 brief 同因：查询边界归一到 UTC，兼容 SQLite 墙钟存储
                events = await self._calendar.list_events(
                    user_id,
                    starts_from=day_end.astimezone(UTC),
                    starts_to=tomorrow_end.astimezone(UTC),
                    include_cancelled=False,
                    limit=50,
                )
            except Exception:
                events = []
            for event in sorted(
                (item for item in events if not item.all_day),
                key=lambda item: item.starts_at,
            ):
                mirror_tag = f"[{event.source}] " if event.source and event.source != "api" else ""
                items.append(
                    ReviewItem(
                        section="tomorrow",
                        text=(
                            f"{mirror_tag}{event.starts_at.astimezone(self._tz).strftime('%H:%M')}"
                            f" {event.title}"
                        ),
                        source=f"calendar:{event.id}",
                    )
                )
            for event in (item for item in events if item.all_day):
                items.append(
                    ReviewItem(
                        section="tomorrow",
                        text=f"全天：{event.title}",
                        source=f"calendar:{event.id}",
                    )
                )

        return _cap_sections(items)

    async def correct_item(
        self,
        user_id: UUID,
        review_id: UUID,
        index: int,
        *,
        action: Literal["confirmed", "removed"],
        note: str | None = None,
    ) -> ReviewView:
        """逐项修正：只改回顾条目的动作/附注并重渲染正文，不动其他任何数据。"""
        async with self._database.sessions.begin() as session:
            record = await session.get(DailyReviewRecord, review_id)
            if record is None or record.user_id != user_id:
                raise LookupError("daily review not found")
            items = [ReviewItem.model_validate(item) for item in record.items]
            if index < 0 or index >= len(items):
                raise ValueError("review item index out of range")
            items[index] = items[index].model_copy(update={"action": action, "note": note})
            record.items = [item.model_dump(mode="json") for item in items]
            record.text = compose_review_text(record.review_date, items)
            record.updated_at = self._clock()
        return _to_view(record)

    async def deliver(
        self,
        user_id: UUID,
        *,
        deliverer: ReviewDeliverer,
        review_date: date | None = None,
    ) -> ReviewView:
        """投递当晚回顾；pending→delivered 乐观转移保证每晚最多一次。"""
        review = await self.build(user_id, review_date=review_date)
        if review.status == "delivered":
            return review
        now = self._clock()
        channels: list[str] | None = None
        try:
            channels = await deliverer(
                review.text,
                user_id=user_id,
                review_id=review.id,
                privacy_level=PrivacyLevel.L1,
                trigger_kind=TRIGGER_KIND_REVIEW,
            )
        except Exception:
            channels = None
        async with self._database.sessions.begin() as session:
            await session.execute(
                update(DailyReviewRecord)
                .where(
                    DailyReviewRecord.id == review.id,
                    DailyReviewRecord.status == "pending",
                )
                .values(
                    status="delivered",
                    delivered_at=now,
                    channels=channels or [],
                    updated_at=now,
                )
            )
        updated = await self.get_review(user_id, review.review_date)
        assert updated is not None
        return updated


def _cap_sections(items: list[ReviewItem]) -> list[ReviewItem]:
    capped: list[ReviewItem] = []
    counts: dict[str, int] = {}
    for item in items:
        count = counts.get(item.section, 0)
        if count >= MAX_ITEMS_PER_SECTION:
            continue
        counts[item.section] = count + 1
        capped.append(item)
    return capped


def compose_review_text(review_date: date, items: list[ReviewItem]) -> str:
    """确定性拼装：removed 的条目不再出现，note 作为更正附注展示。"""
    weekdays = "一二三四五六日"
    header = (
        f"{review_date.month}月{review_date.day}日（周{weekdays[review_date.weekday()]}）晚间回顾"
    )
    lines: list[str] = [header]
    visible = [item for item in items if item.action != "removed"]
    for section in ("completed", "unfinished", "new_commitment", "tomorrow"):
        section_items = [item for item in visible if item.section == section]
        if not section_items:
            continue
        lines.append(f"{_SECTION_TITLES[section]}（{len(section_items)}）：")
        for item in section_items:
            line = f"· {item.text}"
            if item.note:
                line += f"（用户更正：{item.note}）"
            lines.append(line)
    if len(lines) == 1:
        lines.append("今天没有需要回顾的事项。")
    text = "\n".join(lines)
    if len(text) > MAX_TEXT_CHARS:
        text = text[: MAX_TEXT_CHARS - 1] + "…"
    return text
