"""BRIEF-01 每日智能简报：确定性事实采集 + 可溯源拼装 + 每日一次投递。

设计取舍：简报正文用确定性模板拼装而不是 LLM 生成——任务标题、目标标题
都是用户文本，进入模型提示词会引入 Prompt Injection 面且输出不可溯源；
模板拼装天然满足“每条结论可查看来源”与“无重要内容时保持简短”。

事实来源（v1）：
- weather：高德实时天气 + 当日预报（默认城市来自 config.tools.query.default_city）；
- task：TaskStore 当天会触发的活跃时间任务（source: task:{id}）；
- goal：CognitiveStore 当天到期或已过期的活跃承诺（source: goal:{id}）。
日程（J4 日历）、家庭状态与通勤在对应真源接入后再扩展，不伪造数据。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.cognition.store import CognitiveStore
from app.db import AppUserRecord, DailyBriefRecord, Database
from app.ids import uuid7
from app.schemas.common import PrivacyLevel, StrictModel
from app.tasks.models import TaskStatus
from app.tasks.store import TaskStore

TRIGGER_KIND_BRIEF = "brief.daily"
MAX_TASK_FACTS = 5
MAX_GOAL_FACTS = 5
MAX_TEXT_CHARS = 1_200


class BriefFact(StrictModel):
    kind: Annotated[str, Field(min_length=1, max_length=24)]
    text: Annotated[str, Field(min_length=1, max_length=240)]
    source: Annotated[str, Field(min_length=1, max_length=120)]


class BriefView(StrictModel):
    id: UUID
    user_id: UUID
    brief_date: date
    facts: list[BriefFact]
    text: str
    status: Annotated[str, Field(pattern="^(pending|delivered)$")]
    delivered_at: datetime | None = None
    channels: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BriefWeather:
    city: str
    condition: str
    temperature_c: str
    low_c: str | None
    high_c: str | None


BriefWeatherFetcher = Callable[[], Awaitable[BriefWeather | None]]

BriefDeliverer = Callable[..., Awaitable[list[str] | None]]


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _to_view(record: DailyBriefRecord) -> BriefView:
    channels = record.channels or []
    return BriefView(
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


class DailyBriefService:
    def __init__(
        self,
        database: Database,
        task_store: TaskStore,
        cognitive_store: CognitiveStore,
        *,
        weather_fetcher: BriefWeatherFetcher | None = None,
        timezone_name: str = "Asia/Shanghai",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._tasks = task_store
        self._goals = cognitive_store
        self._weather = weather_fetcher
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
        return _to_view(record) if record is not None else None

    async def latest_brief(self, user_id: UUID) -> BriefView | None:
        async with self._database.sessions() as session:
            record = await session.scalar(
                select(DailyBriefRecord)
                .where(DailyBriefRecord.user_id == user_id)
                .order_by(DailyBriefRecord.brief_date.desc())
                .limit(1)
            )
        return _to_view(record) if record is not None else None

    async def recent_briefs(self, user_id: UUID, *, limit: int = 30) -> list[BriefView]:
        async with self._database.sessions() as session:
            records = await session.scalars(
                select(DailyBriefRecord)
                .where(DailyBriefRecord.user_id == user_id)
                .order_by(DailyBriefRecord.brief_date.desc())
                .limit(limit)
            )
        return [_to_view(record) for record in records]

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

    async def collect_facts(self, user_id: UUID, *, brief_date: date) -> list[BriefFact]:
        day_start = datetime.combine(brief_date, dt_time.min, tzinfo=self._tz)
        day_end = day_start + timedelta(days=1)
        facts: list[BriefFact] = []

        weather: BriefWeather | None = None
        if self._weather is not None:
            try:
                weather = await self._weather()
            except Exception:
                weather = None
        if weather is not None:
            summary = f"{weather.city} {weather.condition} {weather.temperature_c}°C"
            if weather.low_c and weather.high_c:
                summary += f"（今日 {weather.low_c}~{weather.high_c}°C）"
            facts.append(BriefFact(kind="weather", text=summary, source="amap:weather"))

        due_tasks = []
        for task in await self._tasks.list_tasks(user_id, status=TaskStatus.ACTIVE, limit=200):
            next_fire = _aware(task.next_fire_at)
            if next_fire is None or task.trigger.type != "time":
                continue
            if day_start <= next_fire < day_end:
                due_tasks.append((next_fire, task))
        due_tasks.sort(key=lambda item: item[0])
        for next_fire, task in due_tasks[:MAX_TASK_FACTS]:
            facts.append(
                BriefFact(
                    kind="task",
                    text=f"{next_fire.astimezone(self._tz).strftime('%H:%M')} {task.title}",
                    source=f"task:{task.id}",
                )
            )

        overdue_goals = []
        for goal in await self._goals.active_goals(user_id, now=self._clock()):
            due = _aware(goal.due_at)
            if due is None or due >= day_end:
                continue
            overdue_goals.append((due, goal))
        overdue_goals.sort(key=lambda item: item[0])
        for due, goal in overdue_goals[:MAX_GOAL_FACTS]:
            prefix = "已到期" if due < day_start else due.astimezone(self._tz).strftime("%H:%M")
            facts.append(
                BriefFact(
                    kind="goal",
                    text=f"[{prefix}] {goal.title}",
                    source=f"goal:{goal.id}",
                )
            )
        return facts

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
        now = self._clock()
        channels: list[str] | None = None
        try:
            channels = await deliverer(
                brief.text,
                user_id=user_id,
                brief_id=brief.id,
                privacy_level=PrivacyLevel.L1,
                trigger_kind=TRIGGER_KIND_BRIEF,
            )
        except Exception:
            channels = None
        async with self._database.sessions.begin() as session:
            await session.execute(
                update(DailyBriefRecord)
                .where(
                    DailyBriefRecord.id == brief.id,
                    DailyBriefRecord.status == "pending",
                )
                .values(
                    status="delivered",
                    delivered_at=now,
                    channels=channels or [],
                    updated_at=now,
                )
            )
        updated = await self.get_brief(user_id, brief.brief_date)
        assert updated is not None
        return updated


def compose_brief_text(brief_date: date, facts: list[BriefFact]) -> str:
    """确定性拼装：无重要内容时保持一行短句。"""
    weekdays = "一二三四五六日"
    header = f"{brief_date.month}月{brief_date.day}日 周{weekdays[brief_date.weekday()]}"

    weather = [fact.text for fact in facts if fact.kind == "weather"]
    tasks = [fact.text for fact in facts if fact.kind == "task"]
    goals = [fact.text for fact in facts if fact.kind == "goal"]

    lines: list[str] = []
    if weather:
        lines.append(f"{header} · {weather[0]}")
    else:
        lines.append(header)
    if tasks:
        lines.append(f"今日待办（{len(tasks)}）：")
        lines.extend(f"· {item}" for item in tasks)
    if goals:
        lines.append(f"到期承诺（{len(goals)}）：")
        lines.extend(f"· {item}" for item in goals)
    if not tasks and not goals:
        lines.append("今天没有到期的任务或承诺。")
    text = "\n".join(lines)
    if len(text) > MAX_TEXT_CHARS:
        text = text[: MAX_TEXT_CHARS - 1] + "…"
    return text
