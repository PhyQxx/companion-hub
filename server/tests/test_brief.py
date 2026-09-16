from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import create_briefs_router
from app.auth import AuthService
from app.calendar import CalendarStore
from app.cognition import CognitiveStore, GoalKind
from app.db import AppUserRecord, Base, Database, create_database
from app.ids import uuid7
from app.tasks.brief import (
    BriefFact,
    BriefWeather,
    BriefWeatherFetcher,
    DailyBriefService,
    compose_brief_text,
)
from app.tasks.brief_scheduler import DailyBriefScheduler
from app.tasks.models import TaskKind, TaskTrigger
from app.tasks.store import TaskStore

NOW = datetime(2026, 9, 2, 1, 0, tzinfo=UTC)  # Asia/Shanghai 当天 09:00


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    value = create_database("sqlite+aiosqlite:///:memory:")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield value
    finally:
        await value.close()


@pytest.fixture
async def user_id(database: Database) -> UUID:
    value = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=value, display_name="Brief owner", status="active"))
    return value


def _service(
    database: Database,
    *,
    weather: BriefWeatherFetcher | None = None,
    calendar_store: CalendarStore | None = None,
    commute_fetcher: Callable[[UUID], Any] | None = None,
    clock: Callable[[], datetime] = lambda: NOW,
) -> DailyBriefService:
    return DailyBriefService(
        database,
        TaskStore(database),
        CognitiveStore(database),
        calendar_store=calendar_store,
        weather_fetcher=weather,
        commute_fetcher=commute_fetcher,
        clock=clock,
    )


async def _make_weather() -> BriefWeather:
    return BriefWeather(city="杭州", condition="多云", temperature_c="26", low_c="20", high_c="31")


# ---------------------------------------------------------------------------
# 事实采集与拼装
# ---------------------------------------------------------------------------


async def test_collect_facts_aggregates_tasks_and_goals(database: Database, user_id: UUID) -> None:
    tasks = TaskStore(database)
    await tasks.create(
        user_id=user_id,
        kind=TaskKind.REMINDER,
        title="上午取快递",
        trigger=TaskTrigger(type="time", at=NOW + timedelta(hours=2)),
        now=NOW,
    )
    await tasks.create(
        user_id=user_id,
        kind=TaskKind.REMINDER,
        title="明天的提醒不进今天",
        trigger=TaskTrigger(type="time", at=NOW + timedelta(days=1, hours=2)),
        now=NOW,
    )
    goals = CognitiveStore(database)
    await goals.create_goal(
        user_id=user_id,
        kind=GoalKind.USER,
        title="还图书馆的书",
        source_kind="manual",
        source_id="manual:b1",
        due_at=NOW + timedelta(hours=3),
    )
    service = _service(database)
    facts = await service.collect_facts(user_id, brief_date=date(2026, 9, 2))
    kinds = [fact.kind for fact in facts]
    assert kinds.count("task") == 1
    assert kinds.count("goal") == 1
    task_fact = next(fact for fact in facts if fact.kind == "task")
    assert "取快递" in task_fact.text
    assert task_fact.source.startswith("task:")
    goal_fact = next(fact for fact in facts if fact.kind == "goal")
    assert goal_fact.source.startswith("goal:")


async def test_collect_facts_includes_weather_with_source(
    database: Database, user_id: UUID
) -> None:
    async def weather() -> BriefWeather:
        return await _make_weather()

    service = _service(database, weather=weather)
    facts = await service.collect_facts(user_id, brief_date=date(2026, 9, 2))
    weather_fact = next(fact for fact in facts if fact.kind == "weather")
    assert "杭州" in weather_fact.text
    assert weather_fact.source == "amap:weather"


async def test_collect_facts_weather_failure_yields_no_fact(
    database: Database, user_id: UUID
) -> None:
    async def broken() -> BriefWeather | None:
        raise RuntimeError("amap down")

    service = _service(database, weather=broken)
    facts = await service.collect_facts(user_id, brief_date=date(2026, 9, 2))
    assert not [fact for fact in facts if fact.kind == "weather"]


def test_compose_brief_text_short_when_empty() -> None:
    text = compose_brief_text(date(2026, 9, 2), [])
    assert text == "9月2日 周三\n今天没有到期的任务或承诺。"


def test_compose_brief_text_sections_and_weather_header() -> None:
    facts = [
        BriefFact(kind="weather", text="杭州 多云 26°C", source="amap:weather"),
        BriefFact(kind="task", text="09:30 站会", source="task:1"),
        BriefFact(kind="goal", text="[14:00] 还书", source="goal:2"),
    ]
    text = compose_brief_text(date(2026, 9, 2), facts)
    assert text.startswith("9月2日 周三 · 杭州 多云 26°C")
    assert "今日待办（1）：" in text
    assert "· 09:30 站会" in text
    assert "到期承诺（1）：" in text


async def test_overdue_goal_marked_in_brief(database: Database, user_id: UUID) -> None:
    goals = CognitiveStore(database)
    await goals.create_goal(
        user_id=user_id,
        kind=GoalKind.USER,
        title="过期未完成的承诺",
        source_kind="manual",
        source_id="manual:b2",
        due_at=NOW - timedelta(days=1),
    )
    service = _service(database)
    facts = await service.collect_facts(user_id, brief_date=date(2026, 9, 2))
    goal_fact = next(fact for fact in facts if fact.kind == "goal")
    assert goal_fact.text.startswith("[已到期]")


# ---------------------------------------------------------------------------
# 幂等构建与投递
# ---------------------------------------------------------------------------


async def test_build_is_idempotent_per_day(database: Database, user_id: UUID) -> None:
    service = _service(database)
    first = await service.build(user_id, brief_date=date(2026, 9, 2))
    second = await service.build(user_id, brief_date=date(2026, 9, 2))
    assert first.id == second.id
    assert first.status == "pending"
    # 手动生成不投递；第二天会新建
    next_day = await service.build(user_id, brief_date=date(2026, 9, 3))
    assert next_day.id != first.id


async def test_deliver_once_per_day_and_records_channels(database: Database, user_id: UUID) -> None:
    service = _service(database)
    calls: list[str] = []

    async def deliverer(text: str, **kwargs: object) -> list[str]:
        calls.append(text)
        return ["web_chat"]

    delivered = await service.deliver(user_id, deliverer=deliverer, brief_date=date(2026, 9, 2))
    again = await service.deliver(user_id, deliverer=deliverer, brief_date=date(2026, 9, 2))
    assert delivered.status == "delivered"
    assert delivered.channels == ["web_chat"]
    assert again.status == "delivered"
    assert len(calls) == 1


async def test_deliverer_failure_still_marks_delivered_once(
    database: Database, user_id: UUID
) -> None:
    service = _service(database)

    async def broken(text: str, **kwargs: object) -> list[str]:
        raise RuntimeError("no channel")

    delivered = await service.deliver(user_id, deliverer=broken, brief_date=date(2026, 9, 2))
    assert delivered.status == "delivered"
    assert delivered.channels == []


# ---------------------------------------------------------------------------
# 调度器
# ---------------------------------------------------------------------------


async def test_brief_scheduler_time_gate_and_run_once(database: Database, user_id: UUID) -> None:
    service = _service(database)
    delivered_texts: list[str] = []

    async def deliverer(text: str, **kwargs: object) -> list[str]:
        delivered_texts.append(text)
        return ["web_chat"]

    scheduler = DailyBriefScheduler(
        service,
        brief_time=dt_time(8, 0),
        clock=lambda: NOW,
        deliverer=deliverer,
    )
    # 时刻未到（本地 07:30）不投递
    early = await scheduler.run_once(now=datetime(2026, 9, 1, 23, 30, tzinfo=UTC))
    assert early == 0
    # 到点后投递一次；重复运行不再投
    assert await scheduler.run_once(now=NOW) == 1
    assert await scheduler.run_once(now=NOW + timedelta(minutes=10)) == 0
    assert len(delivered_texts) == 1


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


async def test_briefs_api_latest_generate_and_auth(database: Database) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="Brief user", password="correct horse")
    service = _service(database)
    app = FastAPI()
    app.include_router(create_briefs_router(service, auth))
    headers = {"Authorization": f"Bearer {owner.access_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        empty = await client.get("/api/v1/briefs/latest", headers=headers)
        generated = await client.post("/api/v1/briefs/generate", headers=headers)
        latest = await client.get("/api/v1/briefs/latest", headers=headers)
        listed = await client.get("/api/v1/briefs", headers=headers)
        unauthorized = await client.get("/api/v1/briefs/latest")
    assert empty.status_code == 200 and empty.json() is None
    assert generated.status_code == 200
    assert generated.json()["brief_date"] == "2026-09-02"
    assert latest.json()["id"] == generated.json()["id"]
    assert [item["id"] for item in listed.json()["items"]] == [generated.json()["id"]]
    assert unauthorized.status_code == 401


# ---------------------------------------------------------------------------
# 当日日程事实（本地 + CalDAV/Google 镜像）
# ---------------------------------------------------------------------------


async def test_brief_includes_today_events_from_calendar(
    database: Database, user_id: UUID
) -> None:
    from app.calendar import CalendarStore

    store = CalendarStore(database)
    day = NOW.astimezone(ZoneInfo("Asia/Shanghai")).date()
    # SQLite 丢时区（读回按 UTC）：用 UTC 瞬间构造，上海本地显示 = UTC+8
    day_start_utc = datetime.combine(day, dt_time.min, tzinfo=UTC) - timedelta(hours=8)
    day_start = day_start_utc + timedelta(hours=8)
    await store.create_event(
        user_id=user_id,
        title="周会",
        starts_at=day_start_utc + timedelta(hours=10),
        ends_at=day_start_utc + timedelta(hours=11),
        location="301 会议室",
    )
    await store.create_event(
        user_id=user_id,
        title="团队日",
        starts_at=day_start_utc,
        ends_at=day_start_utc + timedelta(days=1),
        all_day=True,
    )
    # 昨天的日程不进今天的简报
    await store.create_event(
        user_id=user_id,
        title="昨天的事",
        starts_at=day_start_utc - timedelta(hours=2),
        ends_at=day_start_utc - timedelta(hours=1),
    )
    from app.db import CalendarEventRecord

    async with database.sessions.begin() as session:
        session.add(
            CalendarEventRecord(
                id=uuid7(),
                user_id=user_id,
                calendar_id="caldav:personal",
                title="牙医",
                starts_at=day_start_utc + timedelta(hours=15),
                ends_at=day_start_utc + timedelta(hours=16),
                participants=[],
                status="active",
                source="caldav",
                source_ref="caldav:single-1#20260920T170000",
            )
        )

    brief = await _service(
        database, calendar_store=store, clock=lambda: day_start + timedelta(hours=1)
    ).build(user_id, brief_date=day)
    event_facts = [fact for fact in brief.facts if fact.kind == "event"]
    texts = [fact.text for fact in event_facts]
    assert any("10:00-11:00 周会" in text and "@301 会议室" in text for text in texts)
    assert any("[caldav] 15:00-16:00 牙医" in text for text in texts)
    assert any(text.startswith("全天：团队日") for text in texts)
    assert not any("昨天的事" in text for text in texts)
    # 每条日程事实带可核对的来源引用
    sources = {fact.source for fact in event_facts}
    assert all(source.startswith("calendar:") for source in sources)
    assert len(sources) == 3

    text = brief.text
    assert "今日日程" in text or any("周会" in line for line in text.splitlines())


async def test_brief_without_calendar_store_skips_events(
    database: Database, user_id: UUID
) -> None:
    brief = await _service(database).build(user_id)
    assert not [fact for fact in brief.facts if fact.kind == "event"]


# ---------------------------------------------------------------------------
# 通勤建议事实
# ---------------------------------------------------------------------------


async def test_brief_includes_commute_suggestion(database: Database, user_id: UUID) -> None:
    from datetime import datetime as _dt

    from app.tasks.brief import BriefCommute

    async def commute(user_id: UUID):
        return BriefCommute(
            destination="公司",
            leave_by=_dt(2026, 9, 2, 1, 40, tzinfo=UTC),
            event_title="周会",
            starts_at=_dt(2026, 9, 2, 2, 0, tzinfo=UTC),
            mode="driving",
            duration_min=20,
        )

    brief = await _service(database, commute_fetcher=commute).build(user_id)
    commute_facts = [fact for fact in brief.facts if fact.kind == "commute"]
    assert len(commute_facts) == 1
    text = commute_facts[0].text
    assert "09:40 出发前往 公司" in text
    assert "周会 10:00 开始" in text
    assert "driving约 20 分钟" in text
    assert commute_facts[0].source == "commute:周会"
    assert "出行建议：" in brief.text

    # 事件获取失败 → 静默降级，无通勤事实
    def broken(user_id: UUID):
        raise RuntimeError("amap down")

    # build 按日幂等：换一天验证失败降级
    empty = await _service(database, commute_fetcher=broken).build(
        user_id, brief_date=date(2026, 9, 3)
    )
    assert not [fact for fact in empty.facts if fact.kind == "commute"]
