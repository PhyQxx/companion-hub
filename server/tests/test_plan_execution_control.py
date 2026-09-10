"""PC-02 执行可视化：执行中停止、迟到结果不推进、执行进度事件与视图字段。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from app.api import create_cognition_router
from app.auth import AuthService
from app.cognition import (
    ActionInvocation,
    ActionPlanService,
    ActionPlanStatus,
    ActionStepStatus,
    ActionStepView,
    PlanExecutionEvent,
    build_builtin_action_registry,
)
from app.cognition.store import CognitiveStore
from app.db import (
    ActionPlanRecord,
    AppUserRecord,
    Base,
    Database,
    create_database,
)
from app.ids import uuid7
from app.tools import ToolResult


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
        session.add(AppUserRecord(id=value, display_name="Plan owner", status="active"))
    return value


class BlockingRunner:
    """可选阻塞第一步到测试放行；记录每次调用以断言「迟到结果不推进」。"""

    def __init__(self, *, first_step_fails: bool = False, block: bool = True) -> None:
        self.gate = asyncio.Event()
        self.calls: list[int] = []
        self.first_step_fails = first_step_fails
        self.block = block

    async def __call__(self, step: ActionStepView, user_id: UUID) -> ToolResult:
        self.calls.append(step.position)
        if step.position == 1 and self.block:
            await self.gate.wait()
        if step.position == 1 and self.first_step_fails:
            return ToolResult(
                ok=False,
                tool_name=step.tool_name,
                reason_code="runner_failed",
                latency_ms=1,
            )
        return ToolResult(ok=True, tool_name=step.tool_name, latency_ms=1)


def _service(database: Database, runner: BlockingRunner) -> ActionPlanService:
    return ActionPlanService(
        database,
        build_builtin_action_registry(),
        preauthorized_action_ids=frozenset({"home.light.turn_off"}),
        runner=runner,
    )


async def _ready_two_step_plan(
    database: Database, user_id: UUID, service: ActionPlanService
) -> UUID:
    plan = await service.create_plan(
        user_id=user_id,
        title="睡眠准备",
        invocations=[
            ActionInvocation(action_id="home.light.turn_off", arguments={"target": "书房灯"}),
            ActionInvocation(action_id="home.light.turn_off", arguments={"target": "客厅灯"}),
        ],
        ttl_seconds=600,
    )
    if plan.status == ActionPlanStatus.AWAITING_CONFIRMATION:
        confirmed = await service.confirm_plan(user_id=user_id, plan_id=plan.id)
        assert confirmed.status == ActionPlanStatus.READY
    return plan.id


async def test_cancel_during_execution_stops_new_steps_and_records_inflight(
    database: Database, user_id: UUID
) -> None:
    runner = BlockingRunner()
    service = _service(database, runner)
    plan_id = await _ready_two_step_plan(database, user_id, service)

    events: list[PlanExecutionEvent] = []

    async def collect(event: PlanExecutionEvent) -> None:
        events.append(event)

    service.set_execution_listener(collect)

    started = asyncio.Event()

    async def notify_start(event: PlanExecutionEvent) -> None:
        events.append(event)
        if event.phase == "step.started":
            started.set()

    service.set_execution_listener(notify_start)
    execute_task = asyncio.create_task(service.execute_plan(user_id=user_id, plan_id=plan_id))
    await asyncio.wait_for(started.wait(), timeout=5)

    cancelled = await service.cancel_plan(user_id=user_id, plan_id=plan_id)
    assert cancelled.cancel_requested is True
    assert cancelled.status == ActionPlanStatus.EXECUTING.value  # 协作标记，未立刻翻状态

    runner.gate.set()
    view = await asyncio.wait_for(execute_task, timeout=5)

    assert view.status == ActionPlanStatus.CANCELLED
    assert view.steps[0].status == ActionStepStatus.COMPLETED.value  # 在途步骤如实记录
    assert view.steps[1].status == ActionStepStatus.CANCELLED.value
    # 迟到结果不推进：第二步从未被调用
    assert runner.calls == [1]
    await asyncio.sleep(0.05)
    assert runner.calls == [1]


async def test_failed_inflight_step_with_cancel_intent_ends_cancelled(
    database: Database, user_id: UUID
) -> None:
    runner = BlockingRunner(first_step_fails=True)
    service = _service(database, runner)
    plan_id = await _ready_two_step_plan(database, user_id, service)

    started = asyncio.Event()

    async def notify_start(event: PlanExecutionEvent) -> None:
        if event.phase == "step.started":
            started.set()

    service.set_execution_listener(notify_start)
    execute_task = asyncio.create_task(service.execute_plan(user_id=user_id, plan_id=plan_id))
    await asyncio.wait_for(started.wait(), timeout=5)

    await service.cancel_plan(user_id=user_id, plan_id=plan_id)
    runner.gate.set()
    view = await asyncio.wait_for(execute_task, timeout=5)

    # 用户取消意图优先：在途步骤失败 + 取消请求 → 计划 cancelled（不是 failed）
    assert view.status == ActionPlanStatus.CANCELLED
    assert view.steps[0].status == ActionStepStatus.FAILED.value
    assert view.steps[1].status == ActionStepStatus.CANCELLED.value


async def test_execution_events_carry_progress_and_evidence_status(
    database: Database, user_id: UUID
) -> None:
    runner = BlockingRunner(block=False)
    service = _service(database, runner)
    plan_id = await _ready_two_step_plan(database, user_id, service)

    events: list[PlanExecutionEvent] = []

    async def collect(event: PlanExecutionEvent) -> None:
        events.append(event)

    service.set_execution_listener(collect)

    view = await service.execute_plan(user_id=user_id, plan_id=plan_id)
    assert view.status == ActionPlanStatus.COMPLETED

    phases = [event.phase for event in events]
    assert phases == [
        "execution.started",
        "step.started",
        "step.finished",
        "step.started",
        "step.finished",
        "execution.finished",
    ]
    assert events[0].total == 2
    assert events[2].completed == 1
    assert events[2].step_status == ActionStepStatus.COMPLETED.value
    assert events[2].step_verification is not None  # 证据状态随事件透出
    assert events[-1].plan_status == ActionPlanStatus.COMPLETED.value
    assert events[-1].completed == 2


async def test_listener_failure_never_breaks_execution(database: Database, user_id: UUID) -> None:
    runner = BlockingRunner(block=False)
    service = _service(database, runner)
    plan_id = await _ready_two_step_plan(database, user_id, service)

    async def broken_listener(event: PlanExecutionEvent) -> None:
        raise RuntimeError("listener down")

    service.set_execution_listener(broken_listener)
    view = await service.execute_plan(user_id=user_id, plan_id=plan_id)
    assert view.status == ActionPlanStatus.COMPLETED


async def test_cognition_api_cancel_accepts_executing_plan(
    database: Database, user_id: UUID
) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="Plan user", password="correct horse")
    service = ActionPlanService(database, build_builtin_action_registry())
    plan = await service.create_plan(
        user_id=owner.principal.user_id,
        invocations=[
            ActionInvocation(action_id="home.light.turn_off", arguments={"target": "书房灯"})
        ],
    )
    confirmed = await service.confirm_plan(user_id=owner.principal.user_id, plan_id=plan.id)

    # 手动推进到 executing（模拟已进入执行）
    async with database.sessions.begin() as session:
        await session.execute(
            update(ActionPlanRecord)
            .where(ActionPlanRecord.id == confirmed.id)
            .values(status=ActionPlanStatus.EXECUTING.value, confirmed_at=datetime.now(UTC))
        )

    app = FastAPI()
    app.include_router(
        create_cognition_router(
            CognitiveStore(database),
            auth,
            action_registry=build_builtin_action_registry(),
            action_plan_service=service,
        )
    )
    headers = {"Authorization": f"Bearer {owner.access_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/cognition/action-plans/{confirmed.id}/cancel",
            headers=headers,
            json={"reason": "changed my mind"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["cancel_requested"] is True
        assert body["status"] == ActionPlanStatus.EXECUTING.value
        assert body["cancel_reason"] == "changed my mind"


async def test_cancel_at_execution_start_prevents_first_step(
    database: Database, user_id: UUID
) -> None:
    runner = BlockingRunner(block=False)
    service = _service(database, runner)
    plan_id = await _ready_two_step_plan(database, user_id, service)

    async def listener(event: PlanExecutionEvent) -> None:
        if event.phase == "execution.started":
            await service.cancel_plan(user_id=user_id, plan_id=event.plan_id)

    service.set_execution_listener(listener)
    result = await service.execute_plan(user_id=user_id, plan_id=plan_id)
    assert result.status == "cancelled"
    assert runner.calls == []
