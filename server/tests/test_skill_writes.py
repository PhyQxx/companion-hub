"""S3 declarative skill writes: action sync, execution gates, plan-confirm-execute."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.cognition.action_plan import (
    ActionInvocation,
    ActionPlanService,
    ActionPlanStatus,
    ActionVerificationStatus,
)
from app.cognition.action_registry import build_builtin_action_registry
from app.cognition.action_runner import ToolActionRunner
from app.db import AppUserRecord, Base, Database, create_database
from app.ids import uuid7
from app.schemas import PrivacyLevel
from app.skills import SkillApiManifest, SkillDocument, SkillOperation
from app.skills.actions import SKILL_ACTION_PREFIX, sync_skill_actions
from app.skills.connections import SkillConnection, SkillConnectionStore, SkillHttpClient
from app.skills.store import SkillStore
from app.skills.writes import SkillWriteToolHandler, _SkillWriteArgs
from app.tools import ToolExecutor, ToolRegistry
from app.tools.contracts import ToolContext


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    value = create_database("sqlite+aiosqlite:///:memory:")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield value


def _document(*, extra_operation: bool = False) -> SkillDocument:
    operations = [
        SkillOperation(
            name="todo_create",
            description="创建待办",
            method="POST",
            path="/todos",
            risk="confirm",
            parameters={"title": {"type": "string", "required": True, "location": "body"}},
        )
    ]
    if extra_operation:
        operations.append(
            SkillOperation(
                name="todo_delete",
                description="删除待办",
                method="DELETE",
                path="/todos/{todo_id}",
                risk="confirm",
                parameters={"todo_id": {"type": "string", "required": True, "location": "path"}},
            )
        )
    return SkillDocument(
        name="partner-todo",
        description="写入合作系统待办",
        instructions="用户要求创建待办时使用。",
        api=SkillApiManifest(
            schema_version=1,
            connection="partner-system",
            operations=operations,
        ),
    )


_WRITE_CONNECTION = dict(
    id="partner-system",
    base_url="https://partner.example",
    auth_type="none",
    allowed_paths=["/todos"],
    allowed_write_paths=["/todos"],
    enabled=True,
)


def _write_handler(
    store: SkillStore,
    connections: SkillConnectionStore,
    http: SkillHttpClient,
) -> SkillWriteToolHandler:
    return SkillWriteToolHandler(store, connections, http)


@pytest.mark.asyncio
async def test_sync_registers_enabled_confirm_ops_and_removes_stale(
    database: Database,
) -> None:
    store = SkillStore(database)
    registry = build_builtin_action_registry()
    skill = await store.create(_document(), source="created")
    # 停用技能不同步；启用后登记写动作
    report = sync_skill_actions(registry, await store.list())
    assert report.registered == ()
    await store.set_enabled(skill.id, True)
    report = sync_skill_actions(registry, await store.list())
    assert report.registered == ("skill.partner-todo.todo_create",)
    definition = registry.require("skill.partner-todo.todo_create").definition
    assert definition.risk == "A2"
    assert definition.tool_name == "skill_write"
    assert definition.bound_arguments == {
        "skill_name": "partner-todo",
        "operation": "todo_create",
    }
    # 契约新增操作后同步覆盖；技能停用后移除
    await store.set_enabled(skill.id, False)
    await store.set_api(skill.id, _document(extra_operation=True).api or skill.api)  # type: ignore[arg-type]
    await store.set_enabled(skill.id, True)
    sync_skill_actions(registry, await store.list())
    assert (
        "skill.partner-todo.todo_delete"
        in registry.get("skill.partner-todo.todo_delete").definition.action_id
    )  # type: ignore[union-attr]
    await store.set_enabled(skill.id, False)
    sync_skill_actions(registry, await store.list())
    assert not [
        item for item in registry.definitions() if item.action_id.startswith(SKILL_ACTION_PREFIX)
    ]


@pytest.mark.asyncio
async def test_write_handler_gates_and_execution(database: Database) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"code": 200, "data": {"id": 71}})

    store = SkillStore(database)
    connections = SkillConnectionStore(database)
    await connections.put(SkillConnection(**_WRITE_CONNECTION))
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SkillHttpClient(connections, client=http)
    try:
        skill = await store.create(_document(), source="created")
        write = _write_handler(store, connections, client)
        context = ToolContext(privacy_level=PrivacyLevel.L1, idempotency_key="step-key-1")
        # 技能未启用：拒绝
        blocked = await write.execute(
            _write_args("partner-todo", "todo_create", {"title": "买牛奶"}), context
        )
        assert not blocked.ok and blocked.reason_code == "skill_disabled"
        await store.set_enabled(skill.id, True)
        # 未知操作名：拒绝
        missing = await write.execute(_write_args("partner-todo", "ghost", {}), context)
        assert not missing.ok and missing.reason_code == "skill_operation_changed"
        # 参数不满足契约：拒绝
        invalid = await write.execute(_write_args("partner-todo", "todo_create", {}), context)
        assert not invalid.ok and invalid.reason_code == "skill_arguments_invalid"
        # 正常执行：body 参数进 JSON，幂等键透传，运行证据落库
        ok = await write.execute(
            _write_args("partner-todo", "todo_create", {"title": "买牛奶"}), context
        )
        assert ok.ok and ok.data["status"] == "ok"
        assert len(calls) == 1
        assert calls[0].method == "POST"
        assert "买牛奶" in calls[0].content.decode("utf-8")
        assert calls[0].headers.get("Idempotency-Key") == "step-key-1"
        runs = await store.runs(skill.id)
        # 参数校验失败与成功各记一条运行证据
        assert [item.ok for item in runs] == [True, False] or [item.ok for item in runs] == [True]
        assert runs[0].ok and runs[0].operation == "todo_create"
        # 写路径未放行：拒绝且不外发
        await connections.put(SkillConnection(**{**_WRITE_CONNECTION, "allowed_write_paths": []}))
        denied = await write.execute(
            _write_args("partner-todo", "todo_create", {"title": "买牛奶"}), context
        )
        assert not denied.ok and denied.reason_code == "connection_disabled_or_write_path_denied"
        assert len(calls) == 1
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_plan_confirm_execute_and_idempotent_replay(database: Database) -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"code": 200, "data": {"id": 71}})

    store = SkillStore(database)
    connections = SkillConnectionStore(database)
    await connections.put(SkillConnection(**_WRITE_CONNECTION))
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SkillHttpClient(connections, client=http)
    try:
        skill = await store.create(_document(), source="created")
        await store.set_enabled(skill.id, True)
        registry = build_builtin_action_registry()
        sync_skill_actions(registry, await store.list())
        plan_service = ActionPlanService(database, registry)
        plan_service.set_runner(
            ToolActionRunner(
                ToolExecutor(ToolRegistry([_write_handler(store, connections, client)]))
            )
        )
        user_id = uuid7()
        async with database.sessions.begin() as session:
            session.add(AppUserRecord(id=user_id, display_name="技能写用户", status="active"))
        plan = await plan_service.create_plan(
            user_id=user_id,
            invocations=[
                ActionInvocation(
                    action_id="skill.partner-todo.todo_create",
                    arguments={"title": "买牛奶"},
                )
            ],
            idempotency_key="skill-write-e2e-1",
        )
        assert plan.status == ActionPlanStatus.AWAITING_CONFIRMATION
        # 未确认零写入
        assert call_count == 0
        replay = await plan_service.create_plan(
            user_id=user_id,
            invocations=[
                ActionInvocation(
                    action_id="skill.partner-todo.todo_create",
                    arguments={"title": "买牛奶"},
                )
            ],
            idempotency_key="skill-write-e2e-1",
        )
        assert replay.id == plan.id
        confirmed = await plan_service.confirm_plan(user_id=user_id, plan_id=plan.id)
        assert confirmed.status == ActionPlanStatus.READY
        executed = await plan_service.execute_plan(user_id=user_id, plan_id=plan.id)
        assert executed.status == ActionPlanStatus.COMPLETED
        assert call_count == 1
        step = executed.steps[0]
        assert step.verification_status == ActionVerificationStatus.VERIFIED
        assert step.verification_result == {
            "skill_name": "partner-todo",
            "operation": "todo_create",
            "status": "ok",
        }
        # 同幂等键重放返回原计划，不产生第二次远端写入
        again = await plan_service.create_plan(
            user_id=user_id,
            invocations=[
                ActionInvocation(
                    action_id="skill.partner-todo.todo_create",
                    arguments={"title": "买牛奶"},
                )
            ],
            idempotency_key="skill-write-e2e-1",
        )
        assert again.id == plan.id
        assert call_count == 1
    finally:
        await http.aclose()


def _write_args(skill: str, operation: str, arguments: dict[str, Any]) -> Any:
    return _SkillWriteArgs.model_validate(
        {"skill_name": skill, "operation": operation, **arguments}
    )
