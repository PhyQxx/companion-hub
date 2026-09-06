"""COMMUTE-01 出行管家：出发时刻计算、提醒联动、工具门禁与可解释来源。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.calendar import CalendarStore
from app.commute import CommuteCheckTool, CommuteRouteError, CommuteService
from app.commute.service import DEFAULT_WITHIN_HOURS
from app.config import CommuteConfig
from app.db import AppUserRecord, Base, Database, create_database
from app.schemas.common import PrivacyLevel
from app.tasks.models import TaskStatus
from app.tasks.store import TaskStore
from app.tools.contracts import ToolContext

NOW = datetime(2026, 9, 6, 2, 0, tzinfo=UTC)  # Asia/Shanghai 当天 10:00
OWNER = UUID("00000000-0000-0000-0000-00000000c0fe")


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
        session.add(AppUserRecord(id=value, display_name="Commute owner", status="active"))
    return value


class FakeAmap:
    def __init__(self, *, duration_s: int = 1800, fail_origin: bool = False) -> None:
        self.duration_s = duration_s
        self.fail_origin = fail_origin
        self.weather_calls = 0

    async def geocode(self, address: str, *, city: str | None = None) -> dict[str, Any]:
        if self.fail_origin and "家" in address:
            raise RuntimeError("geocode_down")
        if "家" in address:
            return {"location": "120.1,30.2", "citycode": "0571", "adcode": "330100"}
        return {
            "location": "120.3,30.4",
            "citycode": "0571",
            "adcode": "330100",
            "formatted_address": "杭州市西湖区某会议中心",
        }

    async def route(
        self, origin: str, destination: str, *, mode: str, **_: object
    ) -> dict[str, Any]:
        return {
            "route": {
                "paths": [
                    {"distance": "12000", "cost": {"duration": str(self.duration_s)}}
                ]
            }
        }

    async def weather(self, adcode: str, *, extensions: str) -> dict[str, Any]:
        self.weather_calls += 1
        return {"lives": [{"weather": "晴", "temperature": "28"}]}

    async def aclose(self) -> None:
        return None


def _service(database: Database, amap: FakeAmap, **overrides: Any) -> CommuteService:
    values: dict[str, Any] = {
        "origin": "家里的地址",
        "mode": "driving",
        "buffer_minutes": 10,
        "default_city": "杭州",
    }
    values.update(overrides)
    return CommuteService(
        CalendarStore(database),
        TaskStore(database),
        amap,
        clock=lambda: NOW,
        **values,
    )


async def _event_with_location(
    database: Database, user_id: UUID, *, hours_ahead: float, title: str = "客户会议"
) -> Any:
    from app.calendar import CalendarStore

    store = CalendarStore(database)
    start = NOW + timedelta(hours=hours_ahead)
    return await store.create_event(
        user_id=user_id,
        title=title,
        starts_at=start,
        ends_at=start + timedelta(hours=1),
        location="西湖区某会议中心",
    )


def _context(user_id: UUID, *, privacy_level: PrivacyLevel = PrivacyLevel.L1) -> ToolContext:
    return ToolContext(
        privacy_level=privacy_level,
        user_id=user_id,
        turn_id=UUID("0198b2f4-3b00-7001-8000-00000000c0f1"),
    )


# ---------------------------------------------------------------------------
# 服务核心
# ---------------------------------------------------------------------------


async def test_next_outing_picks_soonest_event_with_location(
    database: Database, user_id: UUID
) -> None:
    service = _service(database, FakeAmap())
    # 无地点的更近日程不触发出行
    from app.calendar import CalendarStore

    store = CalendarStore(database)
    await store.create_event(
        user_id=user_id,
        title="站会",
        starts_at=NOW + timedelta(minutes=30),
        ends_at=NOW + timedelta(minutes=60),
    )
    await _event_with_location(database, user_id, hours_ahead=3)
    assert await service.next_outing(user_id) is not None

    # 时间窗外不取
    assert await service.next_outing(user_id, within_hours=1) is None
    # 空日历返回 None
    assert await service.next_outing(uuid4()) is None


async def test_plan_commute_computes_explainable_leave_by(
    database: Database, user_id: UUID
) -> None:
    amap = FakeAmap(duration_s=1800)
    service = _service(database, amap, buffer_minutes=10)
    event = await _event_with_location(database, user_id, hours_ahead=3)
    plan = await service.plan_commute(user_id, event)

    # 出发时刻 = 开始 - 耗时(30min) - 缓冲(10min)
    assert plan.leave_by == event.starts_at - timedelta(minutes=40)
    assert plan.duration_s == 1800
    assert plan.distance_m == 12000
    assert plan.duration_source == "amap:driving:1800s"  # 来源可解释
    assert plan.weather_summary == "晴 28°C"
    assert plan.navigation_uri and plan.navigation_uri.startswith("https://uri.amap.com/")
    assert plan.destination_text == "杭州市西湖区某会议中心"
    assert amap.weather_calls == 1

    # 出发提醒挂到 TASK-01，source_ref=commute:{event_id}
    tasks = TaskStore(database)
    assert plan.reminder_task_id is not None
    task = await tasks.get_task(user_id, UUID(plan.reminder_task_id))
    assert task.source_ref == f"commute:{event.id}"
    assert task.next_fire_at == plan.leave_by


async def test_plan_commute_rebuilds_reminder_not_duplicates(
    database: Database, user_id: UUID
) -> None:
    service = _service(database, FakeAmap())
    event = await _event_with_location(database, user_id, hours_ahead=3)
    first = await service.plan_commute(user_id, event)
    second = await service.plan_commute(user_id, event)
    tasks = TaskStore(database)
    active = await tasks.list_tasks(user_id, status=TaskStatus.ACTIVE, limit=50)
    commute_tasks = [item for item in active if item.source_ref == f"commute:{event.id}"]
    assert len(commute_tasks) == 1
    assert second.reminder_task_id not in (None, first.reminder_task_id)  # 撤旧建新


async def test_plan_commute_errors_are_typed(database: Database, user_id: UUID) -> None:
    service = _service(database, FakeAmap(fail_origin=True))
    event = await _event_with_location(database, user_id, hours_ahead=3)
    with pytest.raises(CommuteRouteError, match="origin_geocode_failed"):
        await service.plan_commute(user_id, event)

    broken = FakeAmap()
    broken.route = lambda *a, **k: (_ for _ in ()).throw(AssertionError("bad"))  # type: ignore[method-assign]
    service2 = _service(database, broken)
    with pytest.raises(CommuteRouteError):
        await service2.plan_commute(user_id, event)


async def test_past_leave_by_clamps_to_now(database: Database, user_id: UUID) -> None:
    service = _service(database, FakeAmap(duration_s=7200), buffer_minutes=60)
    event = await _event_with_location(database, user_id, hours_ahead=1)
    plan = await service.plan_commute(user_id, event)
    # 计算出的出发时刻在过去：钳到"尽快出发"
    assert plan.leave_by >= NOW


# ---------------------------------------------------------------------------
# 聊天工具
# ---------------------------------------------------------------------------


async def test_commute_check_returns_plan_and_sets_reminder(
    database: Database, user_id: UUID
) -> None:
    await _event_with_location(database, user_id, hours_ahead=3)
    tool = CommuteCheckTool(lambda: _service(database, FakeAmap()))
    result = await tool.execute(
        tool.arguments_model.model_validate({}), _context(user_id)
    )
    assert result.ok and result.data["has_outing"] is True
    assert result.data["duration_source"] == "amap:driving:1800s"
    assert result.data["leave_by_local"]


async def test_commute_check_no_outing_and_gate_failures(
    database: Database, user_id: UUID
) -> None:
    tool = CommuteCheckTool(lambda: _service(database, FakeAmap()))
    empty = await tool.execute(tool.arguments_model.model_validate({}), _context(user_id))
    assert empty.ok and empty.data["has_outing"] is False

    disabled = CommuteCheckTool(lambda: None)
    rejected = await disabled.execute(
        tool.arguments_model.model_validate({}), _context(user_id)
    )
    assert not rejected.ok and rejected.reason_code == "commute_not_configured"

    for privacy in (PrivacyLevel.L0, PrivacyLevel.L2):
        blocked = await tool.execute(
            tool.arguments_model.model_validate({}), _context(user_id, privacy_level=privacy)
        )
        assert not blocked.ok and blocked.reason_code == "commute_requires_l1"

    anonymous = ToolContext(
        privacy_level=PrivacyLevel.L1, user_id=None, turn_id=None
    )
    missing_user = await tool.execute(
        tool.arguments_model.model_validate({}), anonymous
    )
    assert not missing_user.ok and missing_user.reason_code == "user_missing"


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def test_commute_config_requires_origin_when_enabled() -> None:
    with pytest.raises(ValidationError, match="origin"):
        CommuteConfig.model_validate({"enabled": True})
    config = CommuteConfig.model_validate({"enabled": True, "origin": "公司楼下"})
    assert config.mode == "driving"
    assert config.buffer_minutes == 10


def test_default_within_hours_is_bounded() -> None:
    assert DEFAULT_WITHIN_HOURS == 24
    from app.commute.tools import CommuteCheckArgs

    with pytest.raises(ValidationError):
        CommuteCheckArgs.model_validate({"within_hours": 100})
