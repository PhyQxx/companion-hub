"""REVIEW-01 晚间回顾：四区块确定性采集、逐项修正、每晚一次投递。

区块：完成事项（当日完成的任务/承诺）、未完成计划（到期未完成的任务/
承诺）、新承诺（当日新建）、明日重点（明天触发的任务/到期承诺）。

边界（对应验收）：
- 用户可逐项修正：confirm / remove / 附注 note，修正只落在回顾自身；
- 不直接改写 Persona、Memory 或任务/目标状态——回顾是只读事实 + 修正
  记录，任何沉淀都必须走既有确认链路。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.cognition.store import CognitiveStore
from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, DailyReviewRecord, Database, TaskRunRecord
from app.ids import uuid7
from app.runs.delivery import deliver_once, outcome
from app.runs.delivery_sources import SqlDeliverySourceRepository
from app.schemas.common import PrivacyLevel
from app.tasks.store import TaskStore

from .review_core import MAX_ITEMS_PER_SECTION as MAX_ITEMS_PER_SECTION
from .review_core import MAX_TEXT_CHARS as MAX_TEXT_CHARS
from .review_core import DailyReviewCollector
from .review_core import _aware as _aware
from .review_core import _cap_sections as _cap_sections
from .review_core import compose_review_text as compose_review_text
from .review_models import _SECTION_TITLES as _SECTION_TITLES
from .review_models import ReviewDeliverer as ReviewDeliverer
from .review_models import ReviewItem as ReviewItem
from .review_models import ReviewSection as ReviewSection
from .review_models import ReviewView as ReviewView

TRIGGER_KIND_REVIEW = "review.evening"


def _to_view(record: DailyReviewRecord, run: TaskRunRecord | None = None) -> ReviewView:
    return ReviewView(
        delivery_outcome=outcome(run),
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


class DailyReviewService(DailyReviewCollector):
    def __init__(
        self,
        database: Database,
        task_store: TaskStore,
        cognitive_store: CognitiveStore,
        *,
        calendar_store: Any | None = None,
        timezone_name: str = "Asia/Shanghai",
        clock: Callable[[], datetime] | None = None,
        budget_loader: Callable[[], RunBudgetConfig] | None = None,
    ) -> None:
        self._budget_loader = budget_loader or RunBudgetConfig
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
            run = (
                await session.scalar(
                    select(TaskRunRecord).where(
                        TaskRunRecord.id == record.id,
                        TaskRunRecord.user_id == user_id,
                    )
                )
                if record is not None
                else None
            )
        return _to_view(record, run) if record is not None else None

    async def latest_review(self, user_id: UUID) -> ReviewView | None:
        async with self._database.sessions() as session:
            record = await session.scalar(
                select(DailyReviewRecord)
                .where(DailyReviewRecord.user_id == user_id)
                .order_by(DailyReviewRecord.review_date.desc())
                .limit(1)
            )
            run = (
                await session.scalar(
                    select(TaskRunRecord).where(
                        TaskRunRecord.id == record.id,
                        TaskRunRecord.user_id == user_id,
                    )
                )
                if record is not None
                else None
            )
        return _to_view(record, run) if record is not None else None

    async def recent_reviews(self, user_id: UUID, *, limit: int = 30) -> list[ReviewView]:
        async with self._database.sessions() as session:
            records = list(
                await session.scalars(
                    select(DailyReviewRecord)
                    .where(DailyReviewRecord.user_id == user_id)
                    .order_by(DailyReviewRecord.review_date.desc())
                    .limit(limit)
                )
            )
            runs = {
                run.id: run
                for run in await session.scalars(
                    select(TaskRunRecord).where(
                        TaskRunRecord.user_id == user_id,
                        TaskRunRecord.id.in_([record.id for record in records]),
                    )
                )
            }
        return [_to_view(record, runs.get(record.id)) for record in records]

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
            run = await session.get(TaskRunRecord, record.id)
        return _to_view(record, run)

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

        async def dispatch() -> list[str] | None:
            return await deliverer(
                review.text,
                user_id=user_id,
                review_id=review.id,
                privacy_level=PrivacyLevel.L1,
                trigger_kind=TRIGGER_KIND_REVIEW,
            )

        await deliver_once(
            self._database,
            source_repository=SqlDeliverySourceRepository(DailyReviewRecord, review.id, user_id),
            source_id=review.id,
            user_id=user_id,
            text=review.text,
            entry="review.delivery",
            config=self._budget_loader(),
            dispatch=dispatch,
        )
        updated = await self.get_review(user_id, review.review_date)
        assert updated is not None
        return updated
