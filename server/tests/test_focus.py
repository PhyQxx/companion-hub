"""FOCUS-01 专注守护：分析核心、会话注册表、调度器冷却与工具门禁。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.db import AppUserRecord, Base, Database, create_database
from app.focus import (
    FocusScheduler,
    FocusService,
    FocusStartTool,
    FocusStatusTool,
    FocusStopTool,
    analyze_focus,
    cooldown_passed,
    with_nudge_marked,
)
from app.focus.analysis import FocusObservation, FocusSession
from app.schemas.common import PrivacyLevel
from app.timeline.store import TimelineStore
from app.tools.contracts import ToolContext

NOW = datetime(2026, 9, 6, 6, 0, tzinfo=UTC)


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
    value = uuid4()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=value, display_name="Focus owner", status="active"))
    return value


def _observation(minutes_ago: float, summary: str) -> FocusObservation:
    return FocusObservation(occurred_at=NOW - timedelta(minutes=minutes_ago), summary=summary)


def _session(keywords: tuple[str, ...] = ("写代码",)) -> FocusSession:
    return FocusSession(
        session_id="s1",
        user_id="user-1",
        target="写代码",
        target_keywords=keywords,
        started_at=NOW - timedelta(hours=2),
        ends_at=NOW + timedelta(hours=1),
        nudged_at={},
    )


# ---------------------------------------------------------------------------
# 分析核心（纯函数）
# ---------------------------------------------------------------------------


def test_long_work_signal_after_threshold() -> None:
    observations = [
        _observation(130, "编写后端服务代码"),
        _observation(30, "继续编写后端服务代码"),
    ]
    signals = analyze_focus(observations, now=NOW, long_work_minutes=90)
    kinds = [signal.kind for signal in signals]
    assert "long_work" in kinds
    signal = next(item for item in signals if item.kind == "long_work")
    assert "1 小时 40 分钟" in signal.message
    assert signal.suggestion == "rest"


def test_context_switching_signal_counts_distinct_topics() -> None:
    observations = [
        _observation(25, "阅读邮件列表"),
        _observation(20, "浏览购物网站"),
        _observation(15, "查看股票行情"),
        _observation(10, "整理文件目录"),
        _observation(5, "看短视频网站"),
    ]
    signals = analyze_focus(observations, now=NOW, switch_count=5)
    signal = next(item for item in signals if item.kind == "context_switching")
    assert "5 次" in signal.message


def test_similar_observations_are_one_topic() -> None:
    observations = [
        _observation(25, "编写后端服务代码"),
        _observation(20, "编写后端服务代码"),
        _observation(15, "继续编写后端服务代码"),
    ]
    signals = analyze_focus(observations, now=NOW, switch_count=5)
    assert not any(signal.kind == "context_switching" for signal in signals)


def test_off_target_signal_when_recent_topics_unrelated() -> None:
    observations = [
        _observation(100, "编写后端服务代码"),
        _observation(20, "浏览购物网站"),
        _observation(5, "看短视频网站"),
    ]
    signals = analyze_focus(observations, now=NOW, session=_session(("代码",)))
    signal = next(item for item in signals if item.kind == "off_target")
    assert "写代码" in signal.message


def test_no_off_target_when_target_matches() -> None:
    observations = [
        _observation(20, "编写后端服务代码"),
        _observation(10, "调试后端服务代码"),
    ]
    signals = analyze_focus(observations, now=NOW, session=_session(("代码",)))
    assert not any(signal.kind == "off_target" for signal in signals)


def test_empty_observations_yield_no_signals() -> None:
    assert analyze_focus([], now=NOW) == ()
    assert analyze_focus([_observation(10, "   ")], now=NOW) == ()


# ---------------------------------------------------------------------------
# 会话服务与时间线评估
# ---------------------------------------------------------------------------


async def test_session_lifecycle_replaces_and_expires(
    database: Database, user_id: UUID
) -> None:
    service = FocusService(TimelineStore(database), clock=lambda: NOW)
    service.start_session(
        str(user_id), target="写代码", keywords=["写代码"], duration_minutes=60
    )
    second = service.start_session(
        str(user_id), target="写报告", keywords=["写报告"], duration_minutes=30
    )
    # 新会话替换旧会话
    assert service.get_session(str(user_id)) is second
    assert service.stop_session(str(user_id)) is second
    assert service.get_session(str(user_id)) is None

    service.start_session(str(user_id), target="临时", keywords=[], duration_minutes=15)
    # 时钟推进过到期时间 → 自动清理
    service._clock = lambda: NOW + timedelta(hours=1)
    assert service.get_session(str(user_id)) is None


async def test_evaluate_reads_timeline_and_produces_signals(
    database: Database, user_id: UUID
) -> None:
    # 通过正式写入接口沉淀屏幕观察
    store = TimelineStore(database)
    summaries = ["阅读邮件", "浏览购物网站", "查看股票", "整理文件", "看视频"]
    for index, summary in enumerate(summaries):
        await store.index_screen_observation(
            user_id=user_id,
            observation_id=uuid4(),
            display=0,
            summary=summary,
            privacy_level=PrivacyLevel.L1,
            occurred_at=NOW - timedelta(minutes=20 - index * 4),
        )
    service = FocusService(TimelineStore(database), switch_count=5, clock=lambda: NOW)
    session = service.start_session(
        str(user_id), target="写代码", keywords=["写代码"], duration_minutes=60
    )
    signals = await service.evaluate(session)
    kinds = [signal.kind for signal in signals]
    assert "context_switching" in kinds
    assert "off_target" in kinds


# ---------------------------------------------------------------------------
# 调度器：冷却与投递
# ---------------------------------------------------------------------------


async def test_scheduler_delivers_once_then_cooldown(database: Database, user_id: UUID) -> None:
    service = FocusService(TimelineStore(database), clock=lambda: NOW)
    service.start_session(str(user_id), target="写代码", keywords=["写代码"], duration_minutes=120)
    # 注入稳定的屏幕观察（长时工作跨度 100 分钟）
    store = TimelineStore(database)
    for minutes_ago in (100, 50, 5):
        await store.index_screen_observation(
            user_id=user_id,
            observation_id=uuid4(),
            display=0,
            summary="编写后端服务代码",
            privacy_level=PrivacyLevel.L1,
            occurred_at=NOW - timedelta(minutes=minutes_ago),
        )

    delivered: list[str] = []
    current = {"now": NOW}

    async def deliverer(text: str, **kwargs: object) -> list[str]:
        delivered.append(text)
        return ["web_chat"]

    scheduler = FocusScheduler(
        service,
        interval_seconds=10_000,
        clock=lambda: current["now"],
        sleeper=_noop_sleep,
        deliverer=deliverer,
    )
    first = await scheduler.run_once()
    assert first >= 1
    second = await scheduler.run_once()
    # 冷却期内不重复投递
    assert second == 0
    assert len(delivered) == first

    # 冷却过期后同类型信号可再次提醒
    current["now"] = NOW + timedelta(minutes=25)
    third = await scheduler.run_once()
    assert third >= 1


async def _noop_sleep(_seconds: float) -> None:
    return None


def test_cooldown_guard_logic() -> None:
    session = _session()
    signal = next(
        iter(
            analyze_focus(
                [_observation(100, "工作"), _observation(5, "继续工作")],
                now=NOW,
                long_work_minutes=90,
            )
        )
    )
    assert cooldown_passed(session, signal, now=NOW)
    marked = with_nudge_marked(session, signal, now=NOW)
    assert not cooldown_passed(marked, signal, now=NOW + timedelta(minutes=10))
    assert cooldown_passed(marked, signal, now=NOW + timedelta(minutes=21))


# ---------------------------------------------------------------------------
# 聊天工具
# ---------------------------------------------------------------------------


def _context(user_id: UUID, *, privacy_level: PrivacyLevel = PrivacyLevel.L1) -> ToolContext:
    return ToolContext(
        privacy_level=privacy_level,
        user_id=user_id,
        turn_id=UUID("0198b2f4-3b00-7001-8000-00000000f0c1"),
    )


async def test_focus_tools_lifecycle_and_gates(database: Database, user_id: UUID) -> None:
    service = FocusService(TimelineStore(database), clock=lambda: NOW)
    start = FocusStartTool(service)
    stop = FocusStopTool(service)
    status = FocusStatusTool(service)

    started = await start.execute(
        start.arguments_model.model_validate(
            {"target": "写代码", "keywords": ["代码"], "duration_minutes": 60}
        ),
        _context(user_id),
    )
    assert started.ok and started.data["started"] is True

    active = await status.execute(status.arguments_model.model_validate({}), _context(user_id))
    assert active.ok and active.data["active"] is True

    stopped = await stop.execute(stop.arguments_model.model_validate({}), _context(user_id))
    assert stopped.ok and stopped.data["stopped"] is True
    empty = await status.execute(status.arguments_model.model_validate({}), _context(user_id))
    assert empty.ok and empty.data["active"] is False

    # 再次停止：无会话也如实返回 stopped=False
    again = await stop.execute(stop.arguments_model.model_validate({}), _context(user_id))
    assert again.ok and again.data["stopped"] is False

    # 门禁：L0/L2 拒绝、匿名拒绝
    for privacy in (PrivacyLevel.L0, PrivacyLevel.L2):
        blocked = await start.execute(
            start.arguments_model.model_validate({"target": "X"}),
            _context(user_id, privacy_level=privacy),
        )
        assert not blocked.ok and blocked.reason_code == "focus_requires_l1"
    anonymous = ToolContext(privacy_level=PrivacyLevel.L1, user_id=None, turn_id=None)
    missing = await start.execute(
        start.arguments_model.model_validate({"target": "X"}), anonymous
    )
    assert not missing.ok and missing.reason_code == "user_missing"
