"""FLOW-01 可复用流程：存储唯一性、预览编译校验、两阶段保存工具、计划展开与 API。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api import create_workflows_router
from app.auth import AuthService
from app.cognition.action_plan import ActionPlanService
from app.cognition.action_registry import build_builtin_action_registry
from app.db import AppUserRecord, Base, Database, create_database
from app.ids import uuid7
from app.schemas.common import PrivacyLevel
from app.tools.contracts import ToolContext
from app.workflows import (
    WorkflowRunTool,
    WorkflowSaveTool,
    WorkflowService,
    WorkflowStep,
    WorkflowStore,
)
from app.workflows.models import WorkflowPreview

OWNER = UUID("00000000-0000-0000-0000-00000000cafe")
OTHER = UUID("00000000-0000-0000-0000-00000000caff")

TURN_ID = UUID("0198b2f4-3b00-7001-8000-000000000001")


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
        session.add(AppUserRecord(id=value, display_name="Flow owner", status="active"))
    return value


@pytest.fixture
def context(user_id: UUID) -> ToolContext:
    return ToolContext(
        privacy_level=PrivacyLevel.L1,
        user_id=user_id,
        turn_id=TURN_ID,
        idempotency_key="wf-plan-step-1",
    )


def _steps() -> list[WorkflowStep]:
    return [WorkflowStep(action_id="desktop.app.open", arguments={"app": "Safari"})]


def _service(database: Database) -> WorkflowService:
    plans = ActionPlanService(database, build_builtin_action_registry())
    return WorkflowService(
        WorkflowStore(database),
        build_builtin_action_registry(),
        plans,
    )


# ---------------------------------------------------------------------------
# 存储与服务
# ---------------------------------------------------------------------------


async def test_preview_compiles_and_enriches_steps(database: Database, user_id: UUID) -> None:
    service = _service(database)
    preview = await service.preview(
        name="上班准备",
        description="打开浏览器和通知",
        steps=[
            WorkflowStep(action_id="desktop.app.open", arguments={"app": "Safari"}),
            WorkflowStep(
                action_id="desktop.notification.show",
                arguments={"title": "到岗", "body": "开始工作", "privacy_level": "L0"},
            ),
        ],
    )
    assert isinstance(preview, WorkflowPreview)
    assert [step.label for step in preview.steps] == ["打开桌面应用", "发送桌面通知"]
    assert {step.risk for step in preview.steps} == {"A1"}
    assert preview.steps[0].confirmation_policy == "preauthorized"
    assert preview.steps[1].confirmation_policy == "preauthorized"


async def test_preview_rejects_unknown_action_and_bad_arguments(
    database: Database, user_id: UUID
) -> None:
    service = _service(database)
    with pytest.raises(ValueError, match="未知动作"):
        await service.preview(
            name="X", description=None, steps=[WorkflowStep(action_id="not.registered")]
        )
    with pytest.raises(ValueError):
        await service.preview(
            name="X",
            description=None,
            steps=[WorkflowStep(action_id="desktop.app.open", arguments={"app": 42})],
        )
    with pytest.raises(ValueError, match="1-10"):
        await service.preview(name="X", description=None, steps=[])


async def test_save_list_delete_and_name_uniqueness(database: Database, user_id: UUID) -> None:
    service = _service(database)
    view = await service.save_workflow(
        user_id=user_id, name="上班准备", description=None, steps=_steps()
    )
    with pytest.raises(ValueError, match="同名流程已存在"):
        await service.save_workflow(
            user_id=user_id, name=" 上班准备 ", description=None, steps=_steps()
        )
    assert await service.find_by_name(user_id, "上班准备") is not None
    assert await service.find_by_name(user_id, "不存在的流程") is None

    other_user = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=other_user, display_name="Other", status="active"))
    assert await service.find_by_name(other_user, "上班准备") is None

    listed = await service.list_workflows(user_id)
    assert [item.name for item in listed] == ["上班准备"]
    await service.delete_workflow(user_id, view.id)
    with pytest.raises(LookupError):
        await service.get_workflow(user_id, view.id)
    # 删除后可重建
    await service.save_workflow(user_id=user_id, name="上班准备", description=None, steps=_steps())


async def test_run_expands_into_action_plan(database: Database, user_id: UUID) -> None:
    service = _service(database)
    view = await service.save_workflow(
        user_id=user_id, name="上班准备", description=None, steps=_steps()
    )
    run = await service.run_workflow(user_id, view.id)
    assert run.workflow_id == view.id
    assert run.awaiting_confirmation is True  # A1 未进预授权集合也需确认
    assert run.plan_status == "awaiting_confirmation"

    plans = ActionPlanService(database, build_builtin_action_registry())
    plan = await plans.get_plan(user_id=user_id, plan_id=run.plan_id)
    assert plan.title == "流程：上班准备"
    assert plan.steps[0].arguments == {"app": "Safari"}

    # 幂等：显式同 key 重复运行返回同一计划
    again = await service.run_workflow(user_id, view.id, idempotency_key="workflow-run-fixed-key")
    assert again.plan_id != run.plan_id  # 首次未传 key，两者独立
    third = await service.run_workflow(user_id, view.id, idempotency_key="workflow-run-fixed-key")
    assert third.plan_id == again.plan_id


# ---------------------------------------------------------------------------
# 聊天工具
# ---------------------------------------------------------------------------


async def test_save_tool_requires_preview_confirmation_first(
    database: Database, context: ToolContext
) -> None:
    tool = WorkflowSaveTool(_service(database))
    payload = {"name": "上班准备", "steps": [s.model_dump(mode="json") for s in _steps()]}
    result = await tool.execute(
        tool.arguments_model.model_validate(payload), context
    )
    assert result.ok
    assert result.data["saved"] is False
    assert result.data["confirmation_required"] is True
    preview = result.data["preview"]
    assert preview["steps"][0]["label"] == "打开桌面应用"  # 权限元数据必须完整返回
    assert "risk" in preview["steps"][0]
    # 预览不落库
    assert await _service(database).find_by_name(context.user_id or uuid7(), "上班准备") is None


async def test_save_tool_rejects_model_confirmation_and_ui_confirm_is_idempotent(
    database: Database, context: ToolContext
) -> None:
    tool = WorkflowSaveTool(_service(database))
    payload = {"name": "上班准备", "steps": [s.model_dump(mode="json") for s in _steps()]}
    direct = await tool.execute(
        tool.arguments_model.model_validate({**payload, "confirmed": True}), context
    )
    assert not direct.ok and direct.reason_code == "user_confirmation_required"
    prepared = await tool.execute(tool.arguments_model.model_validate(payload), context)
    assert context.user_id is not None
    draft = tool.list_drafts(context.user_id)[0]
    first = await tool.confirm(
        context.user_id, UUID(str(prepared.data["draft_id"])), str(draft["digest"])
    )
    second = await tool.confirm(
        context.user_id, UUID(str(prepared.data["draft_id"])), str(draft["digest"])
    )
    assert first["result"] == second["result"]


async def test_save_tool_gates_privacy_and_invalid_steps(
    database: Database, user_id: UUID, context: ToolContext
) -> None:
    tool = WorkflowSaveTool(_service(database))
    private = ToolContext(privacy_level=PrivacyLevel.L2, user_id=user_id, turn_id=TURN_ID)
    rejected = await tool.execute(
        tool.arguments_model.model_validate(
            {"name": "X", "steps": [s.model_dump(mode="json") for s in _steps()]}
        ),
        private,
    )
    assert not rejected.ok and rejected.reason_code == "private_session_unsupported"

    invalid = await tool.execute(
        tool.arguments_model.model_validate(
            {"name": "X", "steps": [{"action_id": "not.registered", "arguments": {}}]}
        ),
        context,
    )
    assert not invalid.ok and invalid.reason_code == "invalid_workflow"


async def test_run_tool_finds_by_id_or_name_and_reports_confirmation(
    database: Database, user_id: UUID, context: ToolContext
) -> None:
    service = _service(database)
    view = await service.save_workflow(
        user_id=user_id, name="上班准备", description=None, steps=_steps()
    )
    tool = WorkflowRunTool(service)

    by_id = await tool.execute(
        tool.arguments_model.model_validate({"workflow_id": str(view.id)}), context
    )
    assert by_id.ok and by_id.data["awaiting_confirmation"] is True

    by_name = await tool.execute(
        tool.arguments_model.model_validate({"name": "上班准备"}), context
    )
    # 同 turn 幂等：按名与按 ID 命中同一流程，返回同一计划
    assert by_name.ok and by_name.data["plan_id"] == by_id.data["plan_id"]

    missing = await tool.execute(
        tool.arguments_model.model_validate({"name": "没有的流程"}), context
    )
    assert not missing.ok and missing.reason_code == "workflow_not_found"

    no_target = await tool.execute(tool.arguments_model.model_validate({}), context)
    assert not no_target.ok and no_target.reason_code == "workflow_target_missing"


def test_workflow_step_model_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        WorkflowStep.model_validate({"action_id": "desktop.app.open", "extra": 1})


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


async def test_workflows_api_full_flow(database: Database) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="Flow user", password="correct horse")
    service = _service(database)
    app = FastAPI()
    app.include_router(create_workflows_router(service, auth))
    headers = {"Authorization": f"Bearer {owner.access_token}"}
    steps = [{"action_id": "desktop.app.open", "arguments": {"app": "Safari"}}]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/workflows")).status_code == 401

        previewed = await client.post(
            "/api/v1/workflows/preview", headers=headers, json={"name": "上班准备", "steps": steps}
        )
        assert previewed.status_code == 200
        assert previewed.json()["steps"][0]["label"] == "打开桌面应用"

        bad = await client.post(
            "/api/v1/workflows",
            headers=headers,
            json={"name": "X", "steps": [{"action_id": "nope"}]},
        )
        assert bad.status_code == 422

        created = await client.post(
            "/api/v1/workflows", headers=headers, json={"name": "上班准备", "steps": steps}
        )
        assert created.status_code == 201
        workflow_id = created.json()["id"]

        conflict = await client.post(
            "/api/v1/workflows", headers=headers, json={"name": "上班准备", "steps": steps}
        )
        assert conflict.status_code == 422

        listed = await client.get("/api/v1/workflows", headers=headers)
        assert [item["name"] for item in listed.json()] == ["上班准备"]

        run = await client.post(f"/api/v1/workflows/{workflow_id}/run", headers=headers)
        assert run.status_code == 200
        assert run.json()["awaiting_confirmation"] is True

        deleted = await client.delete(f"/api/v1/workflows/{workflow_id}", headers=headers)
        assert deleted.status_code == 204
        missing = await client.get(f"/api/v1/workflows/{workflow_id}", headers=headers)
        assert missing.status_code == 404
        gone_run = await client.post(f"/api/v1/workflows/{workflow_id}/run", headers=headers)
        assert gone_run.status_code == 404


async def test_workflow_draft_api_confirms_exact_preview_or_cancels(database: Database) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="Flow draft", password="correct horse")
    service = _service(database)
    tool = WorkflowSaveTool(service)
    context = ToolContext(
        privacy_level="L1", user_id=owner.principal.user_id, turn_id=uuid7()
    )
    payload = {
        "name": "卡片确认流程",
        "steps": [{"action_id": "desktop.app.open", "arguments": {"app": "Safari"}}],
    }
    prepared = await tool.execute(tool.arguments_model.model_validate(payload), context)
    draft = tool.list_drafts(owner.principal.user_id)[0]
    app = FastAPI()
    app.include_router(create_workflows_router(service, auth, tool))
    headers = {"Authorization": f"Bearer {owner.access_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listed = await client.get("/api/v1/workflows/drafts", headers=headers)
        confirmed = await client.post(
            f"/api/v1/workflows/drafts/{prepared.data['draft_id']}/confirm",
            headers=headers,
            json={"digest": draft["digest"]},
        )
        second_context = context.model_copy(update={"turn_id": uuid7()})
        second = await tool.execute(
            tool.arguments_model.model_validate({**payload, "name": "可取消流程"}),
            second_context,
        )
        cancelled = await client.post(
            f"/api/v1/workflows/drafts/{second.data['draft_id']}/cancel", headers=headers
        )
        refused = await client.post(
            f"/api/v1/workflows/drafts/{second.data['draft_id']}/confirm",
            headers=headers,
            json={"digest": tool.list_drafts(owner.principal.user_id)[-1]["digest"]},
        )

    assert listed.json()[0]["preview"]["steps"][0]["risk"] == "A1"
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "completed"
    assert cancelled.json()["status"] == "cancelled"
    assert refused.status_code == 409
    assert await service.find_by_name(owner.principal.user_id, "卡片确认流程") is not None
    assert await service.find_by_name(owner.principal.user_id, "可取消流程") is None
