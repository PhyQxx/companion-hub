"""HOME-01 家庭场景：CRUD、时段条件、感知触发幂等、计划展开与 API。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api import create_home_scenes_router
from app.auth import AuthService
from app.cognition.action_plan import ActionPlanService
from app.cognition.action_registry import build_builtin_action_registry
from app.db import AppUserRecord, Base, Database, create_database
from app.home_scene import (
    HomeSceneListTool,
    HomeSceneRunTool,
    HomeSceneService,
    HomeSceneStep,
    HomeSceneStore,
)
from app.home_scene.models import in_window, normalize_window
from app.schemas.common import PrivacyLevel
from app.tools.contracts import ToolContext

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


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
        session.add(AppUserRecord(id=value, display_name="Scene owner", status="active"))
    return value


def _service(database: Database) -> HomeSceneService:
    registry = build_builtin_action_registry()
    plans = ActionPlanService(database, registry)
    return HomeSceneService(HomeSceneStore(database), plans, registry, clock=lambda: NOW)


def _steps() -> list[HomeSceneStep]:
    return [
        HomeSceneStep(action_id="home.light.turn_on", arguments={"target": "客厅主灯"}),
        HomeSceneStep(
            action_id="home.light.set_brightness",
            arguments={"target": "客厅主灯", "brightness_pct": 60},
        ),
    ]


# ---------------------------------------------------------------------------
# 时段条件（纯函数）
# ---------------------------------------------------------------------------


def test_window_normalization_and_validation() -> None:
    assert normalize_window("21:30") == "21:30"
    assert normalize_window("7:05") == "07:05"
    with pytest.raises(ValueError, match="HH:MM"):
        normalize_window("25:00")
    with pytest.raises(ValueError, match="HH:MM"):
        normalize_window("晚上八点")


def test_in_window_supports_overnight_ranges() -> None:
    late_night = datetime(2026, 9, 6, 22, 0, tzinfo=UTC)
    early_morning = datetime(2026, 9, 6, 6, 0, tzinfo=UTC)
    noon = NOW
    assert in_window(noon, "09:00", "18:00") is True
    assert in_window(noon, "13:00", "18:00") is False
    # 跨夜 21:00-07:00
    assert in_window(late_night, "21:00", "07:00") is True
    assert in_window(early_morning, "21:00", "07:00") is True
    assert in_window(noon, "21:00", "07:00") is False
    # 未配置 = 全天
    assert in_window(noon, None, None) is True


# ---------------------------------------------------------------------------
# 服务：CRUD 与触发
# ---------------------------------------------------------------------------


async def test_scene_crud_name_uniqueness_and_toggle(
    database: Database, user_id: UUID
) -> None:
    service = _service(database)
    scene = await service.create_scene(
        user_id=user_id, name="回家模式", trigger="user_arrived_home", steps=_steps()
    )
    with pytest.raises(ValueError, match="同名场景已存在"):
        await service.create_scene(
            user_id=user_id, name="回家模式", trigger="user_arrived_home", steps=_steps()
        )
    with pytest.raises(ValueError, match="1-10"):
        await service.create_scene(
            user_id=user_id, name="空场景", trigger="user_arrived_home", steps=[]
        )
    disabled = await service.set_enabled(user_id, scene.id, enabled=False)
    assert disabled.enabled is False
    enabled_again = await service.set_enabled(user_id, scene.id, enabled=True)
    assert enabled_again.enabled is True
    await service.delete_scene(user_id, scene.id)
    with pytest.raises(LookupError):
        await service.get_scene(user_id, scene.id)


async def test_perception_trigger_expands_into_plan(
    database: Database, user_id: UUID
) -> None:
    service = _service(database)
    await service.create_scene(
        user_id=user_id, name="回家模式", trigger="user_arrived_home", steps=_steps()
    )
    event_id = uuid4()
    triggered = await service.handle_semantic_event(user_id, event_id, "user_arrived_home")
    assert len(triggered) == 1
    assert triggered[0].scene_name == "回家模式"
    assert triggered[0].plan_status in {"awaiting_confirmation", "ready"}

    plans = ActionPlanService(database, build_builtin_action_registry())
    plan = await plans.get_plan(user_id=user_id, plan_id=triggered[0].plan_id)
    assert plan.title == "场景：回家模式"
    assert [step.action_id for step in plan.steps] == [
        "home.light.turn_on",
        "home.light.set_brightness",
    ]

    # 同一感知事件重放：幂等键命中既有计划，绝不重复建计划
    replay = await service.handle_semantic_event(user_id, event_id, "user_arrived_home")
    assert len(replay) == 1
    assert replay[0].plan_id == triggered[0].plan_id
    # 不同事件：正常再触发
    again = await service.handle_semantic_event(user_id, uuid4(), "user_arrived_home")
    assert len(again) == 1


async def test_trigger_respects_time_window_and_other_triggers(
    database: Database, user_id: UUID
) -> None:
    service = _service(database)
    # NOW 是 12:00 UTC；窗口 20:00-07:00 之外不触发
    await service.create_scene(
        user_id=user_id,
        name="夜间回家",
        trigger="user_arrived_home",
        steps=_steps(),
        window_start="20:00",
        window_end="07:00",
    )
    assert await service.handle_semantic_event(user_id, uuid4(), "user_arrived_home") == []

    # 触发器不匹配
    await service.create_scene(
        user_id=user_id, name="离家模式", trigger="user_left_home", steps=_steps()
    )
    triggered = await service.handle_semantic_event(user_id, uuid4(), "user_arrived_home")
    assert triggered == []

    # manual 场景只响应手动运行
    manual = await service.create_scene(
        user_id=user_id, name="观影模式", trigger="manual", steps=_steps()
    )
    assert await service.handle_semantic_event(user_id, uuid4(), "manual") == []
    run = await service.run_manual(user_id, manual.id)
    assert run is not None and run.scene_name == "观影模式"

    # 禁用场景不触发
    await service.set_enabled(user_id, manual.id, enabled=False)
    assert await service.run_manual(user_id, manual.id) is None


# ---------------------------------------------------------------------------
# 聊天工具
# ---------------------------------------------------------------------------


def _context(user_id: UUID, *, privacy_level: PrivacyLevel = PrivacyLevel.L1) -> ToolContext:
    return ToolContext(
        privacy_level=privacy_level,
        user_id=user_id,
        turn_id=UUID("0198b2f4-3b00-7001-8000-00000000e0c1"),
    )


async def test_scene_tools_run_list_and_gates(database: Database, user_id: UUID) -> None:
    service = _service(database)
    scene = await service.create_scene(
        user_id=user_id, name="回家模式", trigger="user_arrived_home", steps=_steps()
    )
    run_tool = HomeSceneRunTool(service)
    list_tool = HomeSceneListTool(service)

    listed = await list_tool.execute(
        list_tool.arguments_model.model_validate({}), _context(user_id)
    )
    assert listed.ok and listed.data["scenes"][0]["name"] == "回家模式"

    run = await run_tool.execute(
        run_tool.arguments_model.model_validate({"scene_id": str(scene.id)}),
        _context(user_id),
    )
    assert run.ok and run.data["awaiting_confirmation"] is True

    missing = await run_tool.execute(
        run_tool.arguments_model.model_validate({"scene_id": str(uuid4())}),
        _context(user_id),
    )
    assert not missing.ok and missing.reason_code == "home_scene_not_found"

    for privacy in (PrivacyLevel.L0, PrivacyLevel.L2):
        blocked = await run_tool.execute(
            run_tool.arguments_model.model_validate({"scene_id": str(scene.id)}),
            _context(user_id, privacy_level=privacy),
        )
        assert not blocked.ok and blocked.reason_code == "home_scene_requires_l1"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


async def test_home_scenes_api_full_flow(database: Database) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="Scene user", password="correct horse")
    service = _service(database)
    app = FastAPI()
    app.include_router(create_home_scenes_router(service, auth))
    headers = {"Authorization": f"Bearer {owner.access_token}"}
    steps = [{"action_id": "home.light.turn_on", "arguments": {"target": "客厅主灯"}}]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/home/scenes")).status_code == 401

        bad = await client.post(
            "/api/v1/home/scenes",
            headers=headers,
            json={"name": "X", "trigger": "user_arrived_home", "steps": [{"action_id": "nope"}]},
        )
        assert bad.status_code == 422

        created = await client.post(
            "/api/v1/home/scenes",
            headers=headers,
            json={
                "name": "回家模式",
                "trigger": "user_arrived_home",
                "window_start": "18:00",
                "window_end": "23:00",
                "steps": steps,
            },
        )
        assert created.status_code == 201
        scene_id = created.json()["id"]
        assert created.json()["window_start"] == "18:00"

        conflict = await client.post(
            "/api/v1/home/scenes",
            headers=headers,
            json={"name": "回家模式", "trigger": "user_arrived_home", "steps": steps},
        )
        assert conflict.status_code == 422

        disabled = await client.post(f"/api/v1/home/scenes/{scene_id}/disable", headers=headers)
        assert disabled.status_code == 200 and disabled.json()["enabled"] is False

        run = await client.post(f"/api/v1/home/scenes/{scene_id}/run", headers=headers)
        assert run.status_code == 404  # 禁用场景手动运行也不可用

        enabled = await client.post(f"/api/v1/home/scenes/{scene_id}/enable", headers=headers)
        assert enabled.status_code == 200

        run2 = await client.post(f"/api/v1/home/scenes/{scene_id}/run", headers=headers)
        assert run2.status_code == 200 and run2.json()["awaiting_confirmation"] is True

        deleted = await client.delete(f"/api/v1/home/scenes/{scene_id}", headers=headers)
        assert deleted.status_code == 204
        gone = await client.get(f"/api/v1/home/scenes/{scene_id}", headers=headers)
        assert gone.status_code == 404


def test_step_model_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        HomeSceneStep.model_validate({"action_id": "home.light.turn_on", "extra": 1})
