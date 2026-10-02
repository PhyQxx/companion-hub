"""BTL-03（docs/09 §1）：propose_action 计划确认闭环与完成主动汇报。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from app.cognition import ActionPlanService, ActionStepView, ProposeActionTool
from app.cognition.action_registry import (
    ActionDefinition,
    ActionRegistry,
    ActionRisk,
    ConfirmationPolicy,
)
from app.cognition.propose import PlanCompletionReporter
from app.db import AppUserRecord, Base, Database, create_database
from app.schemas.common import PrivacyLevel
from app.tools.contracts import ToolContext, ToolResult


@pytest.fixture
async def database(tmp_path: Any) -> AsyncIterator[Database]:
    value = create_database(f"sqlite+aiosqlite:///{tmp_path}/propose.db")
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
        session.add(AppUserRecord(id=value, display_name="Owner", status="active"))
    return value


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str


def _registry() -> ActionRegistry:
    registry = ActionRegistry()
    for action_id, risk, policy in [
        ("test.confirm_write", ActionRisk.A2_CONFIRM, ConfirmationPolicy.ALWAYS),
        ("test.low_write", ActionRisk.A1_LOW, ConfirmationPolicy.NEVER),
        ("test.read", ActionRisk.A0_READ, ConfirmationPolicy.NEVER),
        ("test.never_do", ActionRisk.A3_PROHIBITED, ConfirmationPolicy.PROHIBITED),
    ]:
        registry.register(
            ActionDefinition(
                action_id=action_id,
                label=action_id,
                description=action_id,
                risk=risk,
                confirmation_policy=policy,
                tool_name="test_tool",
                arguments_schema=_Args.model_json_schema(),
                timeout_seconds=10,
            ),
            _Args,
        )
    return registry


def _tool(database: Database) -> tuple[ProposeActionTool, ActionPlanService]:
    registry = _registry()
    service = ActionPlanService(database, registry)
    tool = ProposeActionTool(lambda: service, registry)
    return tool, service


def _context(user_id: UUID, turn_id: UUID, privacy: PrivacyLevel = PrivacyLevel.L1) -> ToolContext:
    return ToolContext(privacy_level=privacy, user_id=user_id, turn_id=turn_id)


async def test_propose_a2_creates_awaiting_confirmation_plan(
    database: Database, user_id: UUID
) -> None:
    tool, service = _tool(database)
    turn_id = uuid4()
    await _source_run(database, turn_id, user_id)
    result = await tool.execute(
        tool.arguments_model.model_validate(
            {
                "action_id": "test.confirm_write",
                "arguments": {"target": "主卧灯"},
                "note": "按你的要求关灯",
            }
        ),
        _context(user_id, turn_id),
    )
    assert result.ok
    plan = await service.get_plan(user_id=user_id, plan_id=UUID(result.data["plan_id"]))
    assert plan is not None
    assert plan.status == "awaiting_confirmation"
    assert plan.steps[0].action_id == "test.confirm_write"
    assert plan.steps[0].risk == "A2"
    assert result.data["ack"] == "已生成待确认的操作计划，请在计划卡片上确认。"

    # 同回合同动作幂等
    again = await tool.execute(
        tool.arguments_model.model_validate(
            {
                "action_id": "test.confirm_write",
                "arguments": {"target": "主卧灯"},
                "note": "再提一次",
            }
        ),
        _context(user_id, turn_id),
    )
    assert again.ok and again.data["plan_id"] == result.data["plan_id"]


async def test_propose_a1_creates_ready_plan(database: Database, user_id: UUID) -> None:
    tool, service = _tool(database)
    turn_id = uuid4()
    await _source_run(database, turn_id, user_id)
    result = await tool.execute(
        tool.arguments_model.model_validate(
            {"action_id": "test.low_write", "arguments": {"target": "x"}, "note": "低风险"}
        ),
        _context(user_id, turn_id),
    )
    assert result.ok
    plan = await service.get_plan(user_id=user_id, plan_id=UUID(result.data["plan_id"]))
    assert plan is not None
    assert plan.status == "ready"


async def test_propose_rejects_prohibited_readonly_unknown_and_bad_args(
    database: Database, user_id: UUID
) -> None:
    tool, _ = _tool(database)
    turn_id = uuid4()

    prohibited = await tool.execute(
        tool.arguments_model.model_validate(
            {"action_id": "test.never_do", "arguments": {"target": "x"}, "note": "n"}
        ),
        _context(user_id, turn_id),
    )
    assert not prohibited.ok and prohibited.reason_code == "action_prohibited"

    readonly = await tool.execute(
        tool.arguments_model.model_validate(
            {"action_id": "test.read", "arguments": {"target": "x"}, "note": "n"}
        ),
        _context(user_id, turn_id),
    )
    assert not readonly.ok and readonly.reason_code == "action_is_readonly"

    unknown = await tool.execute(
        tool.arguments_model.model_validate(
            {"action_id": "test.missing", "arguments": {}, "note": "n"}
        ),
        _context(user_id, turn_id),
    )
    assert not unknown.ok and unknown.reason_code == "action_unknown"

    # 近邻提示：模型抄错 id 时给出最接近的注册动作，下一工具轮可自纠错
    typo = await tool.execute(
        tool.arguments_model.model_validate(
            {"action_id": "test.confirm_writ", "arguments": {"target": "x"}, "note": "n"}
        ),
        _context(user_id, turn_id),
    )
    assert not typo.ok and typo.reason_code == "action_unknown"
    assert "test.confirm_write" in typo.data.get("suggest", [])

    invalid = await tool.execute(
        tool.arguments_model.model_validate(
            {
                "action_id": "test.confirm_write",
                "arguments": {"wrong": 1},
                "note": "n",
            }
        ),
        _context(user_id, turn_id),
    )
    assert not invalid.ok
    assert str(invalid.reason_code).startswith("arguments_invalid")
    # 编译错误细节透传：收尾补全能据此向用户说明缺了什么参数
    assert "target" in str(invalid.data.get("error_detail", ""))


async def test_propose_privacy_and_context_gates(database: Database, user_id: UUID) -> None:
    tool, _ = _tool(database)
    l2 = await tool.execute(
        tool.arguments_model.model_validate(
            {"action_id": "test.low_write", "arguments": {"target": "x"}, "note": "n"}
        ),
        _context(user_id, uuid4(), privacy=PrivacyLevel.L2),
    )
    assert not l2.ok and l2.reason_code == "private_session_unsupported"

    no_turn = await tool.execute(
        tool.arguments_model.model_validate(
            {"action_id": "test.low_write", "arguments": {"target": "x"}, "note": "n"}
        ),
        ToolContext(privacy_level=PrivacyLevel.L1, user_id=user_id),
    )
    assert not no_turn.ok and no_turn.reason_code == "turn_context_missing"


async def test_completion_reporter_delivers_after_plan_completes(
    database: Database, user_id: UUID
) -> None:
    """计划全部执行完成后，汇报经主动通道送达；多槽回调互不影响。"""
    registry = _registry()
    delivered: list[tuple[str, dict[str, Any]]] = []
    distiller_calls: list[UUID] = []

    async def deliver(text: str, **kwargs: Any) -> None:
        delivered.append((text, kwargs))

    async def runner(step: ActionStepView, runner_user_id: UUID) -> ToolResult:
        del runner_user_id
        return ToolResult(ok=True, tool_name=step.tool_name, provider="test", latency_ms=1, data={})

    service = ActionPlanService(database, registry, runner=runner)
    reporter = PlanCompletionReporter(service, deliver=deliver)

    def distiller_stub(plan_id: UUID, plan_user_id: UUID) -> None:
        del plan_user_id
        distiller_calls.append(plan_id)

    service.add_completion_callback(distiller_stub)
    service.add_completion_callback(reporter.on_plan_completed)

    plan = await service.create_plan(
        user_id=user_id,
        title="关灯流程",
        invocations=[
            __import__("app.cognition", fromlist=["ActionInvocation"]).ActionInvocation(
                action_id="test.low_write", arguments={"target": "x"}
            ),
        ],
        idempotency_key="report-plan-0001",
    )
    assert plan.status == "ready"
    await service.execute_plan(user_id=user_id, plan_id=plan.id)
    await reporter.drain()

    assert distiller_calls == [plan.id]
    assert len(delivered) == 1
    text, kwargs = delivered[0]
    assert "关灯流程" in text
    assert "全部成功" in text
    assert kwargs["target_user_id"] == user_id
    assert kwargs["rule_id"] == "plan.completed"


async def _source_run(database: Database, run_id: UUID, user_id: UUID) -> None:
    from datetime import UTC, datetime

    from app.db import TaskRunRecord

    async with database.sessions.begin() as session:
        session.add(
            TaskRunRecord(
                id=run_id,
                user_id=user_id,
                contract={"criterion": "reply_committed", "required_work": []},
                status="running",
                privacy_level="L1",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
