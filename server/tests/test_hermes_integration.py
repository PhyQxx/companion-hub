"""HRM 回归（docs/06 Hermes 外部 Agent 集成）：配置、客户端边界、manager
限流与守望语义、委派工具/动作/聊天门控、Admin 状态端点。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api import create_admin_hermes_router
from app.config import HermesConfig, HubConfig
from app.integrations.hermes import (
    HermesClientError,
    HermesManager,
    HermesManagerError,
)
from app.integrations.hermes.client import (
    extract_run_output,
    extract_run_status,
)
from app.integrations.hermes.handler import HermesTaskHandler
from app.jobs.delegated import DelegCancelled, DelegHandlerError


def make_config(**overrides: Any) -> HermesConfig:
    data: dict[str, Any] = {
        "enabled": True,
        "base_url": "http://10.0.0.2:18643",
        "secret_value": "test-key",
        "allow_insecure_local_http": True,
    }
    data.update(overrides)
    return HermesConfig.model_validate(data)


def make_hub(hermes: HermesConfig | None = None) -> HubConfig:
    return HubConfig.model_validate(
        {
            "schema_version": 1,
            "models": {
                "cloud": {
                    "provider": "openai_compatible",
                    "model": "test-model",
                    "base_url": "https://models.example/v1",
                    "runs_local": False,
                    "max_privacy_level": "L1",
                    "max_context_tokens": 32768,
                    "supports_tool_calling": True,
                    "input_cost_per_million": 0,
                    "output_cost_per_million": 0,
                },
                "local": {
                    "provider": "openai_compatible",
                    "model": "local-model",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "runs_local": True,
                    "max_privacy_level": "L2",
                    "max_context_tokens": 32768,
                    "input_cost_per_million": 0,
                    "output_cost_per_million": 0,
                },
            },
            "routes": {
                "dialogue": {"primary": "cloud"},
                "utility": {"primary": "cloud"},
                "private": {"primary": "local"},
            },
            "hermes": hermes.model_dump() if hermes is not None else {},
        }
    )


def make_store(config: HubConfig) -> Any:
    return SimpleNamespace(current=SimpleNamespace(config=config))


class FakeHermesClient:
    """按脚本回放 run 状态；记录调用供断言。"""

    def __init__(self, *, states: list[dict[str, Any]] | None = None) -> None:
        self.states = list(states or [])
        self.calls: list[tuple[str, str]] = []
        self.submit_error: HermesClientError | None = None
        self.stopped: list[str] = []
        self.session_messages: list[dict[str, Any]] = []

    async def health(self) -> dict[str, Any]:
        self.calls.append(("health", ""))
        return {"status": "ok", "platform": "hermes-agent", "version": "0.20.2"}

    async def capabilities(self) -> dict[str, Any]:
        return {"object": "hermes.api_server.capabilities"}

    async def submit_run(self, instruction: str) -> str:
        self.calls.append(("submit", instruction))
        if self.submit_error is not None:
            raise self.submit_error
        return "run-1"

    async def get_run(self, run_id: str) -> dict[str, Any]:
        self.calls.append(("get", run_id))
        if not self.states:
            raise HermesClientError("hermes_unreachable")
        state = self.states.pop(0)
        if not self.states and state.get("status") not in {
            "completed",
            "failed",
            "cancelled",
            "stopped",
        }:
            # 最后一个非终态后保持终态补齐，避免测试脚本过短卡死
            self.states.append({"status": "completed", "output": "done"})
        return state

    async def get_session_messages(self, session_id: str) -> list[dict[str, Any]]:
        self.calls.append(("messages", session_id))
        return self.session_messages

    async def stop_run(self, run_id: str) -> None:
        self.calls.append(("stop", run_id))
        self.stopped.append(run_id)


def make_manager(
    config: HermesConfig | None = None, client: FakeHermesClient | None = None
) -> HermesManager:
    hub = make_hub(config or make_config())
    holder = {"client": client or FakeHermesClient()}

    def factory(*args: Any, **kwargs: Any) -> FakeHermesClient:
        return holder["client"]

    manager = HermesManager(make_store(hub), client_factory=factory)  # type: ignore[arg-type]
    manager._test_client = holder  # type: ignore[attr-defined]
    return manager


# --------------------------------------------------------------------- #
# 配置校验
# --------------------------------------------------------------------- #


def test_config_requires_url_and_secret_when_enabled() -> None:
    with pytest.raises(ValidationError):
        HermesConfig.model_validate({"enabled": True})
    with pytest.raises(ValidationError):
        HermesConfig.model_validate({"enabled": True, "base_url": "http://127.0.0.1:1"})


def test_config_rejects_public_http() -> None:
    with pytest.raises(ValidationError):
        HermesConfig.model_validate(
            {"enabled": True, "base_url": "http://hermes.example.com", "secret_value": "k"}
        )
    # 公网地址即使显式开启本地明文开关也不放行
    with pytest.raises(ValidationError):
        HermesConfig.model_validate(
            {
                "enabled": True,
                "base_url": "http://hermes.example.com",
                "secret_value": "k",
                "allow_insecure_local_http": True,
            }
        )


def test_config_allows_private_http_with_opt_in() -> None:
    config = make_config()
    assert config.enabled
    assert config.base_url is not None


def test_config_rejects_path_and_credentials() -> None:
    with pytest.raises(ValidationError):
        make_config(base_url="http://10.0.0.2:18643/mcp")
    with pytest.raises(ValidationError):
        make_config(base_url="http://user:pw@10.0.0.2:18643")


def test_config_approval_wait_within_run_max() -> None:
    with pytest.raises(ValidationError):
        make_config(approval_wait_seconds=700)


# --------------------------------------------------------------------- #
# 客户端边界
# --------------------------------------------------------------------- #


def test_extract_run_status_normalizes() -> None:
    assert extract_run_status({"status": "completed"}) == "completed"
    assert extract_run_status({"status": "waiting_for_approval"}) == "waiting_for_approval"
    assert extract_run_status({"status": "queued"}) == "queued"
    assert extract_run_status({"status": "weird-value"}) == "running"
    assert extract_run_status({}) == "running"


def test_extract_run_output_defensive() -> None:
    assert extract_run_output({"output": "hello"}) == "hello"
    messages = [{"role": "user", "content": "u"}, {"role": "assistant", "content": "a"}]
    assert extract_run_output({"messages": messages}) == "a"
    assert extract_run_output({}) == ""


# --------------------------------------------------------------------- #
# Manager：限流与提交语义
# --------------------------------------------------------------------- #


async def test_submit_rejects_when_disabled() -> None:
    manager = make_manager(make_config(enabled=False))
    with pytest.raises(HermesManagerError) as error:
        await manager.submit_task(UUID(int=1), "do something")
    assert error.value.reason_code == "hermes_disabled"


async def test_submit_unknown_when_no_run_id() -> None:
    client = FakeHermesClient()
    client.submit_error = HermesClientError("hermes_timeout")
    manager = make_manager(client=client)
    with pytest.raises(HermesManagerError) as error:
        await manager.submit_task(UUID(int=1), "do something")
    assert error.value.reason_code == "hermes_submit_unknown"
    # 失败也释放并发占用
    assert manager.state().active_counts == {}


async def test_daily_limit_and_concurrency() -> None:
    client = FakeHermesClient(states=[{"status": "completed"}])
    manager = make_manager(
        make_config(daily_run_limit=2, max_concurrent_runs=1), client=client
    )
    user = UUID(int=1)
    await manager.submit_task(user, "first")
    with pytest.raises(HermesManagerError) as error:
        await manager.submit_task(user, "second")
    assert error.value.reason_code == "hermes_concurrency_limited"
    # 释放并发后仍可提交；计数到达日限后拒绝
    await manager.watch_task(user, "run-1")
    await manager.submit_task(user, "second-run")
    with pytest.raises(HermesManagerError) as error:
        await manager.submit_task(user, "third")
    assert error.value.reason_code == "hermes_daily_limit_reached"


async def test_watch_task_completed() -> None:
    client = FakeHermesClient(
        states=[
            {"status": "running"},
            {"status": "completed", "output": "all good"},
        ]
    )
    manager = make_manager(client=client)
    outcome = await manager.watch_task(UUID(int=1), "run-1")
    assert outcome.status == "completed"
    assert outcome.output_text == "all good"


async def test_watch_task_completed_fetches_session_messages() -> None:
    # 0.20.2 实测：completed 状态不含正文，需经会话消息端点取回
    client = FakeHermesClient(
        states=[{"status": "completed", "session_id": "run-1"}]
    )
    client.session_messages = [
        {"role": "assistant", "content": "磁盘已清理 3.2G"},
        {"role": "user", "content": "清理磁盘"},
    ]
    manager = make_manager(client=client)
    outcome = await manager.watch_task(UUID(int=1), "run-1")
    assert outcome.status == "completed"
    assert outcome.output_text == "磁盘已清理 3.2G"
    assert ("messages", "run-1") in client.calls


async def test_watch_task_failed_carries_error_text() -> None:
    client = FakeHermesClient(
        states=[{"status": "failed", "error": "HTTP 429: Rate limit reached"}]
    )
    manager = make_manager(client=client)
    outcome = await manager.watch_task(UUID(int=1), "run-1")
    assert outcome.status == "failed"
    assert "429" in outcome.output_text


async def test_watch_task_approval_timeout_stops_run() -> None:
    client = FakeHermesClient(states=[{"status": "waiting_for_approval"}])
    manager = make_manager(make_config(approval_wait_seconds=0), client=client)
    notified: list[str] = []

    async def notify() -> None:
        notified.append("yes")

    outcome = await manager.watch_task(
        UUID(int=1), "run-1", on_approval_pending=notify
    )
    assert outcome.status == "approval_timeout"
    assert client.stopped == ["run-1"]
    assert notified == []  # approval_wait=0 直接停止，不进入通知分支


async def test_watch_task_cancel_cooperative() -> None:
    client = FakeHermesClient(states=[{"status": "running"}])
    manager = make_manager(client=client)

    async def cancelled() -> bool:
        return True

    outcome = await manager.watch_task(UUID(int=1), "run-1", is_cancel_requested=cancelled)
    assert outcome.status == "cancelled"
    assert client.stopped == ["run-1"]
    assert manager.state().active_counts == {}


async def test_watch_task_failure_maps_reason() -> None:
    client = FakeHermesClient(states=[{"status": "failed", "error": "boom"}])
    manager = make_manager(client=client)
    outcome = await manager.watch_task(UUID(int=1), "run-1")
    assert outcome.status == "failed"


# --------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------- #


def _context(job_id: UUID | None = None, cancel: bool = False) -> Any:
    from app.jobs.delegated import DelegRunContext

    async def is_cancel() -> bool:
        return cancel

    return DelegRunContext(job_id=job_id or uuid4(), is_cancel_requested=is_cancel)


async def test_handler_success_summary() -> None:
    client = FakeHermesClient(states=[{"status": "completed", "output": "ok"}])
    manager = make_manager(client=client)
    handler = HermesTaskHandler(manager)
    result = await handler(
        {"user_id": str(UUID(int=1)), "instruction": "检查磁盘"}, _context()
    )
    assert result["status"] == "completed"
    assert "检查磁盘" in result["summary"]
    assert "ok" in result["summary"]


async def test_handler_submit_unknown_not_retried_automatically() -> None:
    client = FakeHermesClient()
    client.submit_error = HermesClientError("hermes_timeout")
    manager = make_manager(client=client)
    handler = HermesTaskHandler(manager)
    with pytest.raises(DelegHandlerError) as error:
        await handler(
            {"user_id": str(UUID(int=1)), "instruction": "x" * 10}, _context()
        )
    assert error.value.reason_code == "hermes_submit_unknown"


async def test_handler_cancelled_outcome_not_reported() -> None:
    client = FakeHermesClient(states=[{"status": "running"}])
    manager = make_manager(client=client)
    handler = HermesTaskHandler(manager)
    with pytest.raises(DelegCancelled):
        await handler(
            {"user_id": str(UUID(int=1)), "instruction": "x" * 10}, _context(cancel=True)
        )


async def test_handler_rejects_long_instruction() -> None:
    manager = make_manager(make_config(instruction_max_chars=50))
    handler = HermesTaskHandler(manager)
    with pytest.raises(DelegHandlerError) as error:
        await handler(
            {"user_id": str(UUID(int=1)), "instruction": "x" * 51}, _context()
        )
    assert error.value.reason_code == "hermes_instruction_too_long"


async def test_handler_notifies_approval() -> None:
    client = FakeHermesClient(
        states=[
            {"status": "waiting_for_approval"},
            {"status": "waiting_for_approval"},
            {"status": "completed", "output": "resumed"},
        ]
    )
    manager = make_manager(client=client)
    seen: list[tuple[str, UUID, str]] = []

    async def notify(run_id: str, user_id: UUID, preview: str) -> None:
        seen.append((run_id, user_id, preview))

    handler = HermesTaskHandler(manager, notify=notify)
    result = await handler(
        {"user_id": str(UUID(int=7)), "instruction": "整理目录"}, _context()
    )
    assert result["status"] == "completed"
    assert seen and seen[0][0] == "run-1" and seen[0][1] == UUID(int=7)


# --------------------------------------------------------------------- #
# 工具与动作
# --------------------------------------------------------------------- #


async def test_delegate_tool_privacy_and_gating() -> None:
    from app.schemas import PrivacyLevel
    from app.tools.contracts import ToolContext
    from app.tools.hermes import HermesDelegateArgs, HermesDelegateTool

    manager = make_manager()
    engine = FakeEngine()
    tool = HermesDelegateTool(manager, engine)

    l2 = ToolContext(privacy_level=PrivacyLevel.L2, user_id=UUID(int=1))
    result = await tool.execute(HermesDelegateArgs(instruction="x" * 30), l2)
    assert result.ok is False and result.reason_code == "private_session_unsupported"

    l1 = ToolContext(
        privacy_level=PrivacyLevel.L1,
        user_id=UUID(int=1),
        turn_id=UUID(int=2),
        idempotency_key="step-1",
    )
    result = await tool.execute(HermesDelegateArgs(instruction="检查内存占用"), l1)
    assert result.ok is True
    assert engine.submitted[0]["kind"] == "deleg.hermes_task"
    assert engine.idempotency_keys[0] == "hermes-step-1"
    assert result.data["job_id"] == str(UUID(int=99))


class FakeEngine:
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []
        self.idempotency_keys: list[str] = []

    async def submit(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        owner: str | None = None,
        source_turn_id: UUID | None = None,
        resource_class: str | None = None,
        max_attempts: int = 1,
    ) -> Any:
        self.submitted.append({"kind": kind, "payload": payload})
        self.idempotency_keys.append(idempotency_key or "")
        return SimpleNamespaceJob()


class SimpleNamespaceJob:
    id = UUID(int=99)
    status = "queued"


async def test_manager_capability_provider_visibility() -> None:
    enabled = make_manager()
    actions = await enabled.available_actions(UUID(int=1))
    assert [item.capability_id for item in actions] == ["hermes:task"]
    assert "hermes.task" in actions[0].description

    disabled = make_manager(make_config(enabled=False))
    assert await disabled.available_actions(UUID(int=1)) == ()


async def test_status_tool_gating() -> None:
    from app.schemas import PrivacyLevel
    from app.tools.contracts import ToolContext
    from app.tools.hermes import HermesStatusArgs, HermesStatusTool

    tool = HermesStatusTool(make_manager())
    result = await tool.execute(
        HermesStatusArgs(), ToolContext(privacy_level=PrivacyLevel.L1, user_id=UUID(int=1))
    )
    assert result.ok is True
    assert result.data["version"] == "0.20.2"

    disabled = HermesStatusTool(make_manager(make_config(enabled=False)))
    result = await disabled.execute(
        HermesStatusArgs(), ToolContext(privacy_level=PrivacyLevel.L1, user_id=UUID(int=1))
    )
    assert result.ok is False and result.reason_code == "hermes_disabled"


def test_builtin_actions_registered() -> None:
    from app.cognition.action_registry import (
        ActionRisk,
        ConfirmationPolicy,
        build_builtin_action_registry,
    )

    registry = build_builtin_action_registry()
    task = registry.require("hermes.task")
    assert task.definition.risk == ActionRisk.A2_CONFIRM
    assert task.definition.confirmation_policy == ConfirmationPolicy.ALWAYS
    assert task.definition.tool_name == "hermes_delegate"
    query = registry.require("hermes.query")
    assert query.definition.risk == ActionRisk.A0_READ
    assert query.definition.tool_name == "hermes_status"


async def test_action_runner_forces_l1_and_receipt_evidence() -> None:
    from app.cognition.action_runner import ToolActionRunner
    from app.schemas import PrivacyLevel
    from app.tools import ToolExecutor, ToolRegistry
    from app.tools.contracts import ToolContext, ToolResult
    from app.tools.hermes import HermesDelegateArgs, HermesDelegateTool

    manager = make_manager()
    engine = FakeEngine()
    tool = HermesDelegateTool(manager, engine)
    captured: dict[str, Any] = {}

    class SpyTool:
        name = tool.name
        description = tool.description
        arguments_model = HermesDelegateArgs

        def definition(self) -> Any:
            return tool.definition()

        async def execute(self, arguments: Any, context: ToolContext) -> ToolResult:
            captured["privacy"] = context.privacy_level
            return await tool.execute(arguments, context)

    runner = ToolActionRunner(ToolExecutor(ToolRegistry([cast(Any, SpyTool())])))
    step = SimpleNamespaceStep(
        tool_name="hermes_delegate",
        arguments={"instruction": "查看磁盘占用"},
        verification_policy="receipt",
        verifier_id="hermes.run_receipt",
    )
    outcome = await runner(step, UUID(int=1))  # type: ignore[arg-type]
    assert captured["privacy"] == PrivacyLevel.L1
    assert str(outcome.verification_status) == "verified"
    assert outcome.verification_result == {
        "job_id": str(UUID(int=99)),
        "kind": "deleg.hermes_task",
        "status": "queued",
    }


class SimpleNamespaceStep:
    def __init__(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        verification_policy: str,
        verifier_id: str,
    ) -> None:
        self.id = uuid4()
        self.tool_name = tool_name
        self.tool_arguments = arguments
        self.verification_policy = verification_policy
        self.verifier_id = verifier_id
        self.idempotency_key = None


def test_chat_gating_blocks_delegate_and_allows_status_l1() -> None:
    from app.chat.service import _device_tool_ready
    from app.llm import LLMRoute
    from app.schemas import PrivacyLevel

    hub = make_hub(make_config())
    assert (
        _device_tool_ready("hermes_delegate", hub, PrivacyLevel.L1, LLMRoute.DIALOGUE) is False
    )
    assert (
        _device_tool_ready("hermes_status", hub, PrivacyLevel.L1, LLMRoute.DIALOGUE) is True
    )
    assert (
        _device_tool_ready("hermes_status", hub, PrivacyLevel.L2, LLMRoute.DIALOGUE) is False
    )
    disabled = make_hub(make_config(enabled=False))
    assert (
        _device_tool_ready("hermes_status", disabled, PrivacyLevel.L1, LLMRoute.DIALOGUE)
        is False
    )


# --------------------------------------------------------------------- #
# Admin 端点
# --------------------------------------------------------------------- #


async def test_admin_hermes_status_endpoints() -> None:
    headers = {"Authorization": "Bearer admin-secret"}
    # 未装配 manager：固定返回 disabled 空态
    empty_app = FastAPI()
    empty_app.include_router(create_admin_hermes_router(None, admin_token="admin-secret"))
    transport = ASGITransport(app=empty_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/api/v1/admin/hermes/status")
        assert denied.status_code == 401
        response = await client.get("/api/v1/admin/hermes/status", headers=headers)
        assert response.status_code == 200
        assert response.json()["enabled"] is False

    # 装配 manager：探测后返回健康与额度
    app = FastAPI()
    app.include_router(create_admin_hermes_router(make_manager(), admin_token="admin-secret"))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/admin/hermes/status", headers=headers)
        assert response.status_code == 200
        payload = response.json()
        assert payload["enabled"] is True
        assert payload["health"]["status"] == "ok"
        assert payload["health"]["version"] == "0.20.2"
        assert "daily_run_limit" in payload["limits"]
