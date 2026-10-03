"""BRIEF-01 每日智能简报：确定性事实采集 + 可溯源拼装 + 每日一次投递。

设计取舍：简报正文用确定性模板拼装而不是 LLM 生成——任务标题、目标标题
都是用户文本，进入模型提示词会引入 Prompt Injection 面且输出不可溯源；
模板拼装天然满足“每条结论可查看来源”与“无重要内容时保持简短”。

事实来源（v1）：
- weather：高德实时天气 + 当日预报（默认城市来自 config.tools.query.default_city）；
- task：TaskStore 当天会触发的活跃时间任务（source: task:{id}）；
- goal：CognitiveStore 当天到期或已过期的活跃承诺（source: goal:{id}）；
- contact：ContactStore 重要日期落在当日的联系人（source: contact:{id}）。
家庭状态与通勤在对应真源接入后再扩展，不伪造数据。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.cognition.store import CognitiveStore
from app.config.models import RunBudgetConfig
from app.contacts.store import ContactStore
from app.db import AppUserRecord, DailyBriefRecord, Database, TaskRunRecord
from app.ids import uuid7
from app.runs.delivery import deliver_once, outcome
from app.runs.delivery_sources import SqlDeliverySourceRepository
from app.schemas.common import PrivacyLevel
from app.tasks.store import TaskStore

from .brief_core import MAX_CONTACT_DATE_FACTS as MAX_CONTACT_DATE_FACTS
from .brief_core import MAX_EVENT_FACTS as MAX_EVENT_FACTS
from .brief_core import MAX_GOAL_FACTS as MAX_GOAL_FACTS
from .brief_core import MAX_TASK_FACTS as MAX_TASK_FACTS
from .brief_core import MAX_TEXT_CHARS as MAX_TEXT_CHARS
from .brief_core import DailyBriefCollector
from .brief_core import _aware as _aware
from .brief_core import compose_brief_text as compose_brief_text
from .brief_models import BriefCommute as BriefCommute
from .brief_models import BriefDeliverer as BriefDeliverer
from .brief_models import BriefFact as BriefFact
from .brief_models import BriefView as BriefView
from .brief_models import BriefWeather as BriefWeather
from .brief_models import BriefWeatherFetcher as BriefWeatherFetcher

TRIGGER_KIND_BRIEF = "brief.daily"


def _to_view(record: DailyBriefRecord, run: TaskRunRecord | None = None) -> BriefView:
    channels = record.channels or []
    return BriefView(
        delivery_outcome=outcome(run),
        id=record.id,
        user_id=record.user_id,
        brief_date=record.brief_date,
        facts=[BriefFact.model_validate(item) for item in record.facts],
        text=record.text,
        status=record.status,
        delivered_at=_aware(record.delivered_at),
        channels=[str(item) for item in channels],
        created_at=_aware(record.created_at),
    )


class DailyBriefService(DailyBriefCollector):
    def __init__(
        self,
        database: Database,
        task_store: TaskStore,
        cognitive_store: CognitiveStore,
        *,
        contact_store: ContactStore | None = None,
        calendar_store: Any | None = None,
        weather_fetcher: BriefWeatherFetcher | None = None,
        commute_fetcher: Callable[[UUID], Any] | None = None,
        timezone_name: str = "Asia/Shanghai",
        clock: Callable[[], datetime] | None = None,
        budget_loader: Callable[[], RunBudgetConfig] | None = None,
    ) -> None:
        self._budget_loader = budget_loader or RunBudgetConfig
        self._database = database
        self._tasks = task_store
        self._goals = cognitive_store
        self._contacts = contact_store
        self._calendar = calendar_store
        self._weather = weather_fetcher
        self._commute = commute_fetcher
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

    async def get_brief(self, user_id: UUID, brief_date: date) -> BriefView | None:
        async with self._database.sessions() as session:
            record = await session.scalar(
                select(DailyBriefRecord).where(
                    DailyBriefRecord.user_id == user_id,
                    DailyBriefRecord.brief_date == brief_date,
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

    async def latest_brief(self, user_id: UUID) -> BriefView | None:
        async with self._database.sessions() as session:
            record = await session.scalar(
                select(DailyBriefRecord)
                .where(DailyBriefRecord.user_id == user_id)
                .order_by(DailyBriefRecord.brief_date.desc())
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

    async def recent_briefs(self, user_id: UUID, *, limit: int = 30) -> list[BriefView]:
        async with self._database.sessions() as session:
            records = list(
                await session.scalars(
                    select(DailyBriefRecord)
                    .where(DailyBriefRecord.user_id == user_id)
                    .order_by(DailyBriefRecord.brief_date.desc())
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

    async def build(self, user_id: UUID, *, brief_date: date | None = None) -> BriefView:
        """采集事实并落库；同日已存在直接返回（幂等）。"""
        day = brief_date or self._clock().astimezone(self._tz).date()
        existing = await self.get_brief(user_id, day)
        if existing is not None:
            return existing
        facts = await self.collect_facts(user_id, brief_date=day)
        text = compose_brief_text(day, facts)
        record = DailyBriefRecord(
            id=uuid7(),
            user_id=user_id,
            brief_date=day,
            facts=[fact.model_dump(mode="json") for fact in facts],
            text=text,
            status="pending",
            created_at=self._clock(),
            updated_at=self._clock(),
        )
        try:
            async with self._database.sessions.begin() as session:
                session.add(record)
        except IntegrityError:
            existing = await self.get_brief(user_id, day)
            assert existing is not None
            return existing
        return _to_view(record)

    async def deliver(
        self,
        user_id: UUID,
        *,
        deliverer: BriefDeliverer,
        brief_date: date | None = None,
    ) -> BriefView:
        """投递当日简报；pending→delivered 的乐观转移保证每天最多投一次。"""
        brief = await self.build(user_id, brief_date=brief_date)
        if brief.status == "delivered":
            return brief

        async def dispatch() -> list[str] | None:
            return await deliverer(
                brief.text,
                user_id=user_id,
                brief_id=brief.id,
                privacy_level=PrivacyLevel.L1,
                trigger_kind=TRIGGER_KIND_BRIEF,
            )

        await deliver_once(
            self._database,
            source_repository=SqlDeliverySourceRepository(DailyBriefRecord, brief.id, user_id),
            source_id=brief.id,
            user_id=user_id,
            text=brief.text,
            entry="brief.delivery",
            config=self._budget_loader(),
            dispatch=dispatch,
        )
        updated = await self.get_brief(user_id, brief.brief_date)
        assert updated is not None
        return updated
