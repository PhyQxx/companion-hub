# ruff: noqa: RUF001
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from app.api import create_chat_router
from app.auth import AuthService
from app.avatar import AvatarStore
from app.chat import ChatService, RuntimeActionCapability, TurnCancelled
from app.chat.service import _render_mail_send_receipt, _render_pnkx_tool_reply
from app.cognition import (
    ActionDefinition,
    ActionRegistry,
    ActionRisk,
    AttentionEngine,
    CognitiveCycle,
    CognitiveStore,
    ConfirmationPolicy,
    RuleBasedDeliberator,
    WorldStateBuilder,
)
from app.config import DatabaseConfigStore, HubConfig
from app.db import (
    AppUserRecord,
    Base,
    CognitiveDecisionRecord,
    ConversationRecord,
    Database,
    MessageRecord,
    create_database,
)
from app.home_assistant import HomeAssistantState, HomeGetStateTool
from app.ids import uuid7
from app.llm import (
    CompletionRequest,
    CompletionResult,
    LLMRoute,
    LLMRouteExhausted,
    ModelUsage,
    ToolCall,
    ToolDefinition,
)
from app.main import create_app
from app.persona import PersonaConfig, PersonaStore
from app.pnkx import (
    PnkxCreateTool,
    PnkxDeleteTool,
    PnkxReadTool,
    PnkxUpdateTool,
)
from app.schemas import PrivacyLevel
from app.skills.runtime import SkillToolProvider
from app.tasks import ReminderCreateTool
from app.tools import ToolExecution, ToolResult
from app.tools.browser import InspectWebpageTool
from app.tools.screen import CaptureScreenTool


def config_yaml(model: str = "dialogue-v1") -> str:
    return f"""
schema_version: 1
models:
  cloud:
    provider: openai_compatible
    model: {model}
    base_url: https://models.example/v1
    secret_ref: env:MODEL_API_KEY
    runs_local: false
    max_privacy_level: L1
    max_context_tokens: 32768
    input_cost_per_million: 0
    output_cost_per_million: 0
  local:
    provider: openai_compatible
    model: local-model
    base_url: http://127.0.0.1:11434/v1
    runs_local: true
    max_privacy_level: L2
    max_context_tokens: 32768
    input_cost_per_million: 0
    output_cost_per_million: 0
routes:
  dialogue: {{primary: cloud}}
  utility: {{primary: cloud}}
  private: {{primary: local}}
"""


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    result = create_database("sqlite+aiosqlite:///:memory:")
    async with result.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield result
    finally:
        await result.close()


@pytest.fixture
async def store(database: Database, tmp_path: Path) -> DatabaseConfigStore:
    path = tmp_path / "hub.yaml"
    path.write_text(config_yaml(), encoding="utf-8")
    result = DatabaseConfigStore(database, path)
    await result.load()
    return result


class FakeRouter:
    def __init__(self, model: str, requests: list[CompletionRequest]) -> None:
        self._model = model
        self._requests = requests

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self._requests.append(request)
        route = LLMRoute.PRIVATE if request.privacy_level == "L2" else request.route
        return CompletionResult(
            text=f"reply from {self._model}",
            provider="openai_compatible",
            model=self._model,
            endpoint="local" if route is LLMRoute.PRIVATE else "cloud",
            route=route,
            finish_reason="stop",
            usage=ModelUsage(input_tokens=10, output_tokens=4, total_tokens=14),
            latency_ms=12.5,
        )

    async def stream(
        self,
        request: CompletionRequest,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> CompletionResult:
        result = await self.complete(request)
        await on_delta(result.text)
        return result


class FakeCapabilityProvider:
    async def available_actions(self, user_id: UUID) -> list[RuntimeActionCapability]:
        del user_id
        return [
            RuntimeActionCapability(
                capability_id="device.tv.living_room.power",
                label="客厅电视",
                description="可以打开或关闭客厅电视",
            )
        ]


class FakeScreenCapabilityProvider:
    async def available_actions(self, user_id: UUID) -> list[RuntimeActionCapability]:
        del user_id
        return [
            RuntimeActionCapability(
                capability_id=f"{uuid7()}:screen.capture",
                label="我的电脑 · screen.capture",
                description="在线桌面已授权单次屏幕读取",
            )
        ]


async def test_pnkx_tools_are_exposed_to_tool_capable_chat_model(
    database: Database,
    store: DatabaseConfigStore,
) -> None:
    candidate = store.current.config.model_dump(mode="python")
    candidate["models"]["cloud"]["supports_tool_calling"] = True
    draft = await store.create_draft(HubConfig.model_validate(candidate), actor="test")
    await store.publish(draft.version, actor="test")
    service = ChatService(
        database,
        store,
        capability_provider=FakeScreenCapabilityProvider(),
        device_tools=(
            PnkxReadTool.__new__(PnkxReadTool),
            PnkxCreateTool.__new__(PnkxCreateTool),
            PnkxUpdateTool.__new__(PnkxUpdateTool),
            PnkxDeleteTool.__new__(PnkxDeleteTool),
        ),
    )
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="pnkx")

    pending = await service.start_turn(
        conversation.id,
        user_id=user.id,
        text="帮我看看待办",
        privacy_level=PrivacyLevel.L1,
    )

    assert pending.tool_names == (
        "pnkx_read_life",
        "pnkx_create_life",
        "pnkx_update_life",
        "pnkx_delete_life",
    )
    assert {tool.name for tool in pending.request.tools} == {
        "pnkx_read_life",
        "pnkx_create_life",
        "pnkx_update_life",
        "pnkx_delete_life",
    }
    assert "device:test:screen.capture" not in pending.request.messages[0].content


async def test_pnkx_intent_keeps_assistant_tools_without_legacy_pnkx_tools(
    database: Database,
    store: DatabaseConfigStore,
) -> None:
    """技能中心化后无旧版 pnkx_* 工具：pnkx 意图消息不得清空助手工具挂载。

    回归 2026-09-30：日记/待办类消息命中 pnkx 意图后 propose_action 等
    工具被全部过滤，模型只剩文字起草并声称「没有写入工具」。
    """
    candidate = store.current.config.model_dump(mode="python")
    candidate["models"]["cloud"]["supports_tool_calling"] = True
    draft = await store.create_draft(HubConfig.model_validate(candidate), actor="test")
    await store.publish(draft.version, actor="test")
    service = ChatService(
        database,
        store,
        device_tools=(ReminderCreateTool.__new__(ReminderCreateTool),),
    )
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="diary")

    pending = await service.start_turn(
        conversation.id,
        user_id=user.id,
        text="今天的日记重新写一下，内容就是哈哈哈",
        privacy_level=PrivacyLevel.L1,
    )

    assert pending.tool_names == ("reminder_create",)


async def test_action_catalog_rendered_into_system_prompt(
    database: Database,
    store: DatabaseConfigStore,
) -> None:
    """自描述能力：注册表快照进提示词，模型不再依赖会漂移的手写文案。"""

    class _ThingArgs(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        title: str

    registry = ActionRegistry()
    registry.register(
        ActionDefinition(
            action_id="skill.demo.thing_add",
            label="技能 · demo.thing_add",
            description="新增演示记录。",
            risk=ActionRisk.A2_CONFIRM,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            reversible=False,
            tool_name="skill_write",
            arguments_schema=_ThingArgs.model_json_schema(),
            bound_arguments={},
            max_privacy_level=PrivacyLevel.L1,
        ),
        _ThingArgs,
    )
    registry.validate()

    candidate = store.current.config.model_dump(mode="python")
    candidate["models"]["cloud"]["supports_tool_calling"] = True
    draft = await store.create_draft(HubConfig.model_validate(candidate), actor="test")
    await store.publish(draft.version, actor="test")
    service = ChatService(database, store, action_registry=registry)
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="catalog")

    pending = await service.start_turn(
        conversation.id,
        user_id=user.id,
        text="帮我记个演示",
        privacy_level=PrivacyLevel.L1,
    )

    system_prompt = pending.request.messages[0].content
    assert "【可提议的写操作】" in system_prompt
    assert "skill.demo.thing_add" in system_prompt
    assert "title*" in system_prompt


async def test_pnkx_read_uses_matching_skill_instead_of_legacy_token_tool(
    database: Database,
    store: DatabaseConfigStore,
) -> None:
    candidate = store.current.config.model_dump(mode="python")
    candidate["models"]["cloud"]["supports_tool_calling"] = True
    draft = await store.create_draft(HubConfig.model_validate(candidate), actor="test")
    await store.publish(draft.version, actor="test")

    class AnniversarySkill:
        name = "skill.pnkx-commemoration.commemoration_list"

        def definition(self) -> ToolDefinition:
            return ToolDefinition(
                name=self.name,
                description="查询纪念日列表",
                parameters={"type": "object"},
            )

    class MatchingSkills:
        async def select(
            self, text: str, *, privacy_level: PrivacyLevel
        ) -> tuple[AnniversarySkill, ...]:
            assert "纪念日" in text
            assert privacy_level is PrivacyLevel.L1
            return (AnniversarySkill(),)

        async def guidance(self, text: str) -> str:
            return ""

    service = ChatService(
        database,
        store,
        device_tools=(PnkxReadTool.__new__(PnkxReadTool),),
        skill_tools=cast(SkillToolProvider, MatchingSkills()),
    )
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="pnkx skill")

    pending = await service.start_turn(
        conversation.id,
        user_id=user.id,
        text="看看纪念日现在有哪些",
        privacy_level=PrivacyLevel.L1,
    )

    assert pending.tool_names == ("skill.pnkx-commemoration.commemoration_list",)
    assert {tool.name for tool in pending.request.tools} == set(pending.tool_names)


def test_pnkx_tool_result_is_rendered_locally() -> None:
    reply = _render_pnkx_tool_reply(
        ToolResult(
            ok=True,
            tool_name="pnkx_read_life",
            provider="pnkx",
            latency_ms=12,
            data={
                "resource": "todos",
                "total": 12,
                "items": [
                    {"content": f"待办 {index}", "planStartTime": "2026-09-04 09:00:00"}
                    for index in range(1, 7)
                ],
            },
        )
    )

    assert reply.startswith("PNKX 中共有 12 条未完成待办，前 5 条是：")
    assert "1. 待办 1 · 2026-09-04 09:00:00" in reply
    assert "5. 待办 5" in reply
    assert "待办 6" not in reply


def test_pnkx_auth_failure_has_actionable_message() -> None:
    reply = _render_pnkx_tool_reply(
        ToolResult(
            ok=False,
            tool_name="pnkx_read_life",
            provider="pnkx",
            latency_ms=12,
            reason_code="integration_token_rejected",
        )
    )
    assert "集成令牌" in reply
    assert "技能中心连接" in reply
    assert "L1 会话" in reply


def test_pnkx_update_and_delete_results_are_rendered_locally() -> None:
    updated = _render_pnkx_tool_reply(
        ToolResult(
            ok=True,
            tool_name="pnkx_update_life",
            provider="pnkx",
            latency_ms=12,
            data={"resource": "todo", "remote_id": "71"},
        )
    )
    deleted = _render_pnkx_tool_reply(
        ToolResult(
            ok=True,
            tool_name="pnkx_delete_life",
            provider="pnkx",
            latency_ms=9,
            data={"resource": "note", "remote_id": "21"},
        )
    )

    assert updated == "已更新 PNKX 待办。"
    assert deleted == "已删除 PNKX 笔记。"


class FakeBrowserCapabilityProvider:
    async def available_actions(self, user_id: UUID) -> list[RuntimeActionCapability]:
        del user_id
        return [
            RuntimeActionCapability(
                capability_id=f"{uuid7()}:browser.current_tab.read",
                label="我的浏览器 · browser.current_tab.read",
                description="在线浏览器已授权当前标签页读取",
            )
        ]


class FakeHomeCapabilityProvider:
    async def available_actions(self, user_id: UUID) -> list[RuntimeActionCapability]:
        del user_id
        return [
            RuntimeActionCapability(
                capability_id=("home_assistant:light.bedroom:state.read"),
                label="主卧灯",
                description="可以读取这个已授权 Home Assistant 实体的当前状态",
            )
        ]


class FakeHomeStateProvider:
    def __init__(self) -> None:
        from app.config import HomeAssistantEntityConfig

        self.policy = HomeAssistantEntityConfig(
            entity_id="light.bedroom",
            display_name="主卧灯",
            read_allowed=True,
        )

    def resolve(self, target: str):  # type: ignore[no-untyped-def]
        assert target == "主卧灯"
        return self.policy

    def get_state(self, entity_id: str) -> HomeAssistantState:
        assert entity_id == "light.bedroom"
        now = datetime(2026, 8, 25, 1, 0, tzinfo=UTC)
        return HomeAssistantState(entity_id, "on", {}, now, now)


class FakeToolRouter:
    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        if request.tools:
            return CompletionResult(
                text="我先猜一下天气。",
                provider="openai_compatible",
                model="tool-model",
                endpoint="cloud",
                route=request.route,
                finish_reason="tool_calls",
                latency_ms=10,
                tool_calls=[
                    ToolCall(
                        id="call-weather",
                        function={
                            "name": "get_weather",
                            "arguments": {"location": "济南市"},
                        },
                    )
                ],
            )
        return CompletionResult(
            text="济南现在多云，29℃。",
            provider="openai_compatible",
            model="tool-model",
            endpoint="cloud",
            route=request.route,
            finish_reason="stop",
            latency_ms=8,
        )

    async def stream(
        self,
        request: CompletionRequest,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> CompletionResult:
        result = await self.complete(request)
        await on_delta(result.text)
        return result


class FakeToolExecutor:
    async def execute(self, call: ToolCall, context: object) -> ToolExecution:
        del context
        return ToolExecution(
            call_id=call.id,
            result=ToolResult(
                ok=True,
                tool_name=call.function.name,
                provider="amap",
                latency_ms=12,
                data={
                    "resolved_location": {"name": "济南市", "adcode": "370100"},
                    "current": {"weather": "多云", "temperature_c": 29},
                },
            ),
        )


class FakeToolRuntime:
    executor = FakeToolExecutor()

    async def close(self) -> None:
        return None


def _append_chat_delta(target: list[str]) -> Callable[[str], Awaitable[None]]:
    async def append(delta: str) -> None:
        target.append(delta)

    return append


def test_mail_send_receipt_is_deterministic() -> None:
    sent = ToolResult(
        ok=True,
        tool_name="mail_send",
        latency_ms=12,
        data={"sent": True, "recipients": ["friend@example.com"]},
    )
    duplicate = ToolResult(
        ok=True,
        tool_name="mail_send",
        latency_ms=2,
        data={
            "sent": False,
            "duplicate": True,
            "recipients": ["friend@example.com"],
        },
    )
    preview = ToolResult(
        ok=True,
        tool_name="mail_send",
        latency_ms=1,
        data={"sent": False, "confirmation_required": True},
    )

    assert _render_mail_send_receipt(sent) == ("邮件已发送成功。收件人：friend@example.com。")
    assert _render_mail_send_receipt(duplicate) == (
        "这封邮件已经发送成功，本次没有重复发送。收件人：friend@example.com。"
    )
    assert _render_mail_send_receipt(preview) is None


async def create_user(database: Database, name: str = "Test") -> AppUserRecord:
    user = AppUserRecord(id=uuid7(), display_name=name, status="active")
    async with database.sessions.begin() as session:
        session.add(user)
    return user


async def test_chat_persists_turn_and_uses_recent_context(
    database: Database, store: DatabaseConfigStore
) -> None:
    requests: list[CompletionRequest] = []
    service = ChatService(
        database,
        store,
        router_builder=lambda config: FakeRouter(config.models["cloud"].model, requests),
    )
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="Context test")

    first = await service.send_message(
        conversation.id, user_id=user.id, text="hello", privacy_level=PrivacyLevel.L1
    )
    second = await service.send_message(
        conversation.id, user_id=user.id, text="again", privacy_level=PrivacyLevel.L1
    )
    messages = await service.list_messages(conversation.id, user_id=user.id)

    assert [message.role for message in messages] == ["user", "assistant", "user", "assistant"]
    assert [message.seq for message in messages] == [1, 2, 3, 4]
    meta = first.assistant_message.decision_meta
    assert meta is not None
    assert meta["context_budget"] == {}
    assert isinstance(meta["context_sources"], list)
    assert "hello" not in str(meta["context_sources"])
    assert {
        key: value
        for key, value in meta.items()
        if key not in {"context_sources", "context_budget", "task_run_id"}
    } == {
        "schema_version": 1,
        "config_version": 1,
        "persona_version": 0,
        "agent_reply": {
            "schema_version": 1,
            "schema_ref": "aria.agent-reply/1",
            "text": "reply from dialogue-v1",
            "tts_text": "reply from dialogue-v1",
            "emotion": "neutral",
            "expressions": [],
            "actions": [],
            "parse_status": "fallback",
        },
        "endpoint": "cloud",
        "provider": "openai_compatible",
        "model": "dialogue-v1",
        "route": "dialogue",
        "finish_reason": "stop",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
            "estimated_cost": 0,
        },
        "latency_ms": 12.5,
        "recall": {"mode": "working"},
        "runtime_capabilities": [],
    }
    assert [message.content for message in requests[1].messages[1:]] == [
        "hello",
        "reply from dialogue-v1",
        "again",
    ]
    assert second.user_message.turn_id == second.assistant_message.turn_id


async def test_chat_records_passive_cognitive_decision_in_turn_audit(
    database: Database, store: DatabaseConfigStore
) -> None:
    requests: list[CompletionRequest] = []
    cognitive_store = CognitiveStore(database)
    cycle = CognitiveCycle(
        cognitive_store,
        WorldStateBuilder(database, cognitive_store),
        AttentionEngine(),
        RuleBasedDeliberator(),
    )
    service = ChatService(
        database,
        store,
        router_builder=lambda config: FakeRouter(config.models["cloud"].model, requests),
        cognitive_cycle=cycle,
    )
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="Cognitive audit")

    result = await service.send_message(
        conversation.id,
        user_id=user.id,
        text="帮我想想今天先做什么",
        privacy_level=PrivacyLevel.L1,
    )

    cognition = (result.assistant_message.decision_meta or {}).get("cognition")
    assert isinstance(cognition, dict)
    assert cognition["decision"] == "record"
    assert cognition["trigger_kind"] == "user.message_received"
    assert cognition["evidence_ids"] == [str(result.user_message.id)]
    assert "message_length" not in cognition


async def test_proactive_message_is_persisted_in_latest_active_conversation(
    database: Database, store: DatabaseConfigStore
) -> None:
    service = ChatService(database, store)
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="Home")

    result = await service.create_proactive_message(
        "主卧灯已经持续开启较长时间，需要的话可以告诉我关闭它。",
        entity_id="light.bedroom",
        rule_id="light_on_30m",
        trigger_kind="light_on_too_long",
    )

    assert result is not None
    target_user_id, message = result
    assert target_user_id == user.id
    assert message.conversation_id == conversation.id
    assert message.role == "assistant"
    assert message.seq == 1
    assert message.decision_meta is not None
    assert message.decision_meta["kind"] == "home_assistant_proactive"


async def test_proactive_delivery_arbitration_stays_with_target_owner(
    database: Database, store: DatabaseConfigStore
) -> None:
    service = ChatService(database, store)
    owner = await create_user(database, "Owner")
    other = await create_user(database, "Other")
    owner_conversation = await service.create_conversation(user_id=owner.id, title="Owner Home")
    await service.create_conversation(user_id=other.id, title="Other Newer Conversation")

    result = await service.create_proactive_message(
        "欢迎回家。",
        entity_id="person.owner",
        rule_id="perception_user_arrived_home",
        trigger_kind="user_arrived_home",
        target_user_id=owner.id,
    )

    assert result is not None
    target_user_id, message = result
    assert target_user_id == owner.id
    assert message.conversation_id == owner_conversation.id


async def test_start_turn_supports_smaller_context_window_for_voice(
    database: Database, store: DatabaseConfigStore
) -> None:
    requests: list[CompletionRequest] = []
    service = ChatService(
        database,
        store,
        router_builder=lambda config: FakeRouter(config.models["cloud"].model, requests),
    )
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="Voice trim test")
    for index in range(3):
        await service.send_message(
            conversation.id,
            user_id=user.id,
            text=f"第{index}轮",
            privacy_level=PrivacyLevel.L1,
        )

    pending = await service.start_turn(
        conversation.id,
        user_id=user.id,
        text="语音输入",
        privacy_level=PrivacyLevel.L1,
        max_context_messages=2,
        llm_route=LLMRoute.VOICE,
    )

    # 3 轮 send_message + 本条语音输入共 7 条历史; 语音窗口只保留最近 2 条
    assert [message.role for message in pending.request.messages] == [
        "system",
        "assistant",
        "user",
    ]
    assert pending.request.messages[-1].content == "语音输入"
    assert pending.request.route == LLMRoute.VOICE

    full = await service.start_turn(
        conversation.id,
        user_id=user.id,
        text="再来一条",
        privacy_level=PrivacyLevel.L1,
    )
    assert len(full.request.messages) == 1 + 8


async def test_chat_injects_trusted_current_time_in_user_timezone(
    database: Database, store: DatabaseConfigStore
) -> None:
    requests: list[CompletionRequest] = []
    service = ChatService(
        database,
        store,
        router_builder=lambda config: FakeRouter(config.models["cloud"].model, requests),
    )
    user = AppUserRecord(
        id=uuid7(),
        display_name="Tokyo user",
        timezone="Asia/Tokyo",
        status="active",
    )
    async with database.sessions.begin() as session:
        session.add(user)
    conversation = await service.create_conversation(user_id=user.id, title="clock")

    await service.send_message(
        conversation.id,
        user_id=user.id,
        text="现在几点？",
        privacy_level=PrivacyLevel.L1,
    )

    system_prompt = requests[-1].messages[0].content
    assert "【当前时间】" in system_prompt
    assert "用户时区: Asia/Tokyo" in system_prompt
    assert "+09:00" in system_prompt
    assert "不要声称自己无法获知当前时间" in system_prompt


async def test_chat_injects_only_reported_runtime_capabilities(
    database: Database, store: DatabaseConfigStore
) -> None:
    requests: list[CompletionRequest] = []
    service = ChatService(
        database,
        store,
        router_builder=lambda config: FakeRouter(config.models["cloud"].model, requests),
        capability_provider=FakeCapabilityProvider(),
    )
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="capability")

    result = await service.send_message(
        conversation.id,
        user_id=user.id,
        text="不想聊这个了，换个事情做吧",
        privacy_level=PrivacyLevel.L1,
    )

    system_prompt = requests[-1].messages[0].content
    assert "【现实能力边界】" in system_prompt
    assert "device.tv.living_room.power" in system_prompt
    assert "客厅电视" in system_prompt
    assert "不得把角色设定中的场景当作真实能力" in system_prompt
    meta = result.assistant_message.decision_meta or {}
    assert meta["runtime_capabilities"] == ["device.tv.living_room.power"]


async def test_l1_screen_tool_accepts_dialogue_tool_model_and_cloud_vision(
    database: Database,
    store: DatabaseConfigStore,
) -> None:
    candidate = store.current.config.model_dump(mode="python")
    candidate["models"]["cloud"]["supports_tool_calling"] = True
    candidate["models"]["cloud_vision"] = {
        "kind": "vision",
        "provider": "openai_compatible",
        "model": "cloud-vision",
        "base_url": "https://vision.example/v1",
        "secret_ref": "env:MODEL_API_KEY",
        "runs_local": False,
        "max_privacy_level": "L1",
    }
    candidate["capability_models"] = {"vision": "cloud_vision"}
    draft = await store.create_draft(HubConfig.model_validate(candidate), actor="test")
    await store.publish(draft.version, actor="test")
    service = ChatService(
        database,
        store,
        capability_provider=FakeScreenCapabilityProvider(),
        device_tool=CaptureScreenTool.__new__(CaptureScreenTool),
    )
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="screen-l1")

    pending = await service.start_turn(
        conversation.id,
        user_id=user.id,
        text="看一下我的电脑屏幕上是什么",
        privacy_level=PrivacyLevel.L1,
    )

    assert pending.tool_names == ("capture_screen",)
    assert [tool.name for tool in pending.request.tools] == ["capture_screen"]


async def test_l2_screen_tool_requires_device_local_vision_and_tool_model(
    database: Database,
    store: DatabaseConfigStore,
) -> None:
    candidate = store.current.config.model_dump(mode="python")
    candidate["models"]["local"]["supports_tool_calling"] = True
    candidate["models"]["local_vision"] = {
        "kind": "vision",
        "provider": "openai_compatible",
        "model": "local-vision",
        "base_url": "http://127.0.0.1:1234/v1",
        "runs_local": True,
        "max_privacy_level": "L2",
    }
    candidate["capability_models"] = {"vision": "local_vision"}
    draft = await store.create_draft(HubConfig.model_validate(candidate), actor="test")
    await store.publish(draft.version, actor="test")
    service = ChatService(
        database,
        store,
        capability_provider=FakeScreenCapabilityProvider(),
        device_tool=CaptureScreenTool.__new__(CaptureScreenTool),
    )
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="screen")

    pending = await service.start_turn(
        conversation.id,
        user_id=user.id,
        text="看一下我的电脑屏幕上是什么",
        privacy_level=PrivacyLevel.L2,
    )

    assert pending.tool_names == ("capture_screen",)
    assert [tool.name for tool in pending.request.tools] == ["capture_screen"]


async def test_l2_browser_tool_is_exposed_for_current_webpage_intent(
    database: Database,
    store: DatabaseConfigStore,
) -> None:
    candidate = store.current.config.model_dump(mode="python")
    candidate["models"]["local"]["supports_tool_calling"] = True
    draft = await store.create_draft(HubConfig.model_validate(candidate), actor="test")
    await store.publish(draft.version, actor="test")
    service = ChatService(
        database,
        store,
        capability_provider=FakeBrowserCapabilityProvider(),
        device_tools=(InspectWebpageTool.__new__(InspectWebpageTool),),
    )
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="browser")

    pending = await service.start_turn(
        conversation.id,
        user_id=user.id,
        text="看一下我的电脑网页",
        privacy_level=PrivacyLevel.L2,
    )

    assert pending.tool_names == ("inspect_webpage",)
    assert [tool.name for tool in pending.request.tools] == ["inspect_webpage"]


async def test_l1_browser_tool_is_exposed_with_tool_calling_model(
    database: Database,
    store: DatabaseConfigStore,
) -> None:
    candidate = store.current.config.model_dump(mode="python")
    candidate["models"]["cloud"]["supports_tool_calling"] = True
    draft = await store.create_draft(HubConfig.model_validate(candidate), actor="test")
    await store.publish(draft.version, actor="test")
    service = ChatService(
        database,
        store,
        capability_provider=FakeBrowserCapabilityProvider(),
        device_tools=(InspectWebpageTool.__new__(InspectWebpageTool),),
    )
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="browser-l1")

    pending = await service.start_turn(
        conversation.id,
        user_id=user.id,
        text="看一下我的电脑网页",
        privacy_level=PrivacyLevel.L1,
    )

    assert pending.tool_names == ("inspect_webpage",)
    assert [tool.name for tool in pending.request.tools] == ["inspect_webpage"]


async def test_l1_browser_tool_remains_unexposed_without_tool_model(
    database: Database,
    store: DatabaseConfigStore,
) -> None:
    candidate = store.current.config.model_dump(mode="python")
    candidate["models"]["cloud"]["supports_tool_calling"] = False
    draft = await store.create_draft(HubConfig.model_validate(candidate), actor="test")
    await store.publish(draft.version, actor="test")
    service = ChatService(
        database,
        store,
        capability_provider=FakeBrowserCapabilityProvider(),
        device_tools=(InspectWebpageTool.__new__(InspectWebpageTool),),
    )
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="browser-l1")

    pending = await service.start_turn(
        conversation.id,
        user_id=user.id,
        text="看一下我的电脑网页",
        privacy_level=PrivacyLevel.L1,
    )

    assert pending.tool_names == ()
    assert pending.request.tools == []


async def test_home_state_read_is_preexecuted_when_model_emits_no_tool_call(
    database: Database,
    store: DatabaseConfigStore,
) -> None:
    candidate = store.current.config.model_dump(mode="python")
    candidate["models"]["cloud"]["supports_tool_calling"] = True
    candidate["integrations"] = {
        "home_assistant": {
            "enabled": True,
            "base_url": "https://ha.example.test",
            "secret_value": "test-token",
            "entities": [
                {
                    "entity_id": "light.bedroom",
                    "display_name": "主卧灯",
                    "read_allowed": True,
                }
            ],
        }
    }
    draft = await store.create_draft(HubConfig.model_validate(candidate), actor="test")
    await store.publish(draft.version, actor="test")
    requests: list[CompletionRequest] = []
    service = ChatService(
        database,
        store,
        router_builder=lambda config: FakeRouter(config.models["cloud"].model, requests),
        capability_provider=FakeHomeCapabilityProvider(),
        device_tools=(HomeGetStateTool(FakeHomeStateProvider()),),
    )
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="ha read")
    pending = await service.start_turn(
        conversation.id,
        user_id=user.id,
        text="主卧灯现在开着呢吗",
        privacy_level=PrivacyLevel.L1,
    )
    deltas: list[str] = []
    events: list[dict[str, object]] = []

    async def capture_event(event: dict[str, object]) -> None:
        events.append(event)

    result = await service.run_stream(
        pending,
        _append_chat_delta(deltas),
        capture_event,
    )

    assert len(requests) == 1
    assert requests[0].tools == []
    assert requests[0].messages[-1].role == "tool"
    assert '"state":"on"' in requests[0].messages[-1].content
    assert [event["type"] for event in events] == ["tool.started", "tool.finished"]
    assert events[1]["ok"] is True
    meta = result.assistant_message.decision_meta or {}
    calls = meta["tool_calls"]
    assert isinstance(calls, list) and isinstance(calls[0], dict)
    assert calls[0]["tool_name"] == "home_get_state"


async def test_recent_home_device_reference_is_added_to_followup_prompt(
    database: Database,
    store: DatabaseConfigStore,
) -> None:
    candidate = store.current.config.model_dump(mode="python")
    candidate["models"]["cloud"]["supports_tool_calling"] = True
    candidate["integrations"] = {
        "home_assistant": {
            "enabled": True,
            "base_url": "https://ha.example.test",
            "secret_value": "test-token",
            "entities": [
                {
                    "entity_id": "light.bedroom",
                    "display_name": "主卧灯",
                    "read_allowed": True,
                }
            ],
        }
    }
    draft = await store.create_draft(HubConfig.model_validate(candidate), actor="test")
    await store.publish(draft.version, actor="test")
    requests: list[CompletionRequest] = []
    service = ChatService(
        database,
        store,
        router_builder=lambda config: FakeRouter(config.models["cloud"].model, requests),
        capability_provider=FakeHomeCapabilityProvider(),
        device_tools=(HomeGetStateTool(FakeHomeStateProvider()),),
    )
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="ha reference")

    await service.send_message(
        conversation.id,
        user_id=user.id,
        text="主卧灯现在开着吗",
        privacy_level=PrivacyLevel.L1,
    )
    pending = await service.start_turn(
        conversation.id,
        user_id=user.id,
        text="它怎么样了",
        privacy_level=PrivacyLevel.L1,
    )

    system_prompt = pending.request.messages[0].content
    assert "【近期设备指代】" in system_prompt
    assert "主卧灯（light.bedroom）" in system_prompt
    assert "不能视为动作确认" in system_prompt


async def test_terse_followup_retries_previous_home_state_read_deterministically(
    database: Database,
    store: DatabaseConfigStore,
) -> None:
    candidate = store.current.config.model_dump(mode="python")
    candidate["models"]["cloud"]["supports_tool_calling"] = True
    candidate["integrations"] = {
        "home_assistant": {
            "enabled": True,
            "base_url": "https://ha.example.test",
            "secret_value": "test-token",
            "entities": [
                {
                    "entity_id": "light.bedroom",
                    "display_name": "主卧灯",
                    "read_allowed": True,
                }
            ],
        }
    }
    draft = await store.create_draft(HubConfig.model_validate(candidate), actor="test")
    await store.publish(draft.version, actor="test")
    requests: list[CompletionRequest] = []
    builder = lambda config: FakeRouter(config.models["cloud"].model, requests)  # noqa: E731
    user = await create_user(database)
    conversation_service = ChatService(database, store, router_builder=builder)
    conversation = await conversation_service.create_conversation(user_id=user.id, title="ha retry")
    await conversation_service.send_message(
        conversation.id,
        user_id=user.id,
        text="主卧灯现在开着呢吗",
        privacy_level=PrivacyLevel.L1,
    )
    service = ChatService(
        database,
        store,
        router_builder=builder,
        capability_provider=FakeHomeCapabilityProvider(),
        device_tools=(HomeGetStateTool(FakeHomeStateProvider()),),
    )

    result = await service.send_message(
        conversation.id,
        user_id=user.id,
        text="查到了吗",
        privacy_level=PrivacyLevel.L1,
    )

    followup_request = requests[-1]
    assert followup_request.messages[-1].role == "tool"
    assert '"state":"on"' in followup_request.messages[-1].content
    meta = result.assistant_message.decision_meta or {}
    calls = meta["tool_calls"]
    assert isinstance(calls, list) and isinstance(calls[0], dict)
    assert calls[0]["tool_name"] == "home_get_state"


async def test_weather_tool_round_hides_preamble_and_records_redacted_metadata(
    database: Database,
    store: DatabaseConfigStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = store.current.config.model_dump(mode="python")
    candidate["models"]["cloud"]["supports_tool_calling"] = True
    candidate["tools"] = {
        "enabled": True,
        "query": {"default_city": "济南市"},
        "amap": {"enabled": True, "secret_value": "test-only-key"},
    }
    draft = await store.create_draft(HubConfig.model_validate(candidate), actor="test")
    await store.publish(draft.version, actor="test")
    backend = FakeToolRouter()
    monkeypatch.setattr(
        "app.chat.service.build_query_tool_runtime",
        lambda config, secrets: FakeToolRuntime(),
    )
    service = ChatService(database, store, router_builder=lambda config: backend)
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="weather")
    deltas: list[str] = []
    tool_events: list[dict[str, object]] = []

    async def capture_tool_event(event: dict[str, object]) -> None:
        tool_events.append(event)

    pending = await service.start_turn(
        conversation.id,
        user_id=user.id,
        text="济南今天天气怎么样？",
        privacy_level=PrivacyLevel.L1,
    )
    result = await service.run_stream(
        pending,
        _append_chat_delta(deltas),
        capture_tool_event,
    )

    assert [tool.name for tool in pending.request.tools] == [
        "get_location",
        "get_weather",
        "search_nearby",
        "plan_route",
    ]
    assert deltas == ["济南现在多云，29℃。"]
    assert [event["type"] for event in tool_events] == [
        "tool.started",
        "tool.finished",
    ]
    assert tool_events[1]["ok"] is True
    assert len(backend.requests) == 2
    followup = backend.requests[1]
    assert followup.tools == []
    assert followup.tool_choice == "none"
    assert followup.messages[-1].role == "tool"
    assert '"weather":"多云"' in followup.messages[-1].content
    meta = result.assistant_message.decision_meta or {}
    assert meta["tool_calls"] == [
        {
            "call_id": "call-weather",
            "tool_name": "get_weather",
            "latency_ms": 12.0,
            "outcome": "success",
            "reason_code": None,
            "provider": "amap",
            "result_count": 1,
            "cache_hit": False,
            "location_source": None,
        }
    ]
    assert "济南市" not in repr(meta["tool_calls"])
    assert meta["tool_result"] == {
        "kind": "weather",
        "provider": "amap",
        "cache_hit": False,
        "fetched_at": None,
        "report_time": None,
        "location": {"name": "济南市", "adcode": "370100"},
        "current": {"weather": "多云", "temperature_c": 29},
        "forecast": [],
    }


class TextToolCallRouter:
    """模拟只把工具目录注入 prompt、却不解析回 tool_calls 字段的网关。

    模型把调用写成正文本 <get_weather>{...}</get_weather>，正好复现
    「输出内容带着标签且工具从未执行」的线上故障形态。
    """

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        if request.tools:
            return CompletionResult(
                text=('好嘞，我查一下天气。<get_weather>{"location": "济南市"}</get_weather>'),
                provider="openai_compatible",
                model="tool-model",
                endpoint="cloud",
                route=request.route,
                finish_reason="stop",
                latency_ms=10,
            )
        return CompletionResult(
            text="济南现在多云，29℃。",
            provider="openai_compatible",
            model="tool-model",
            endpoint="cloud",
            route=request.route,
            finish_reason="stop",
            latency_ms=8,
        )

    async def stream(
        self,
        request: CompletionRequest,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> CompletionResult:
        result = await self.complete(request)
        await on_delta(result.text)
        return result


async def test_text_form_tool_call_is_absorbed_executed_and_never_leaks(
    database: Database,
    store: DatabaseConfigStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = store.current.config.model_dump(mode="python")
    candidate["models"]["cloud"]["supports_tool_calling"] = True
    candidate["tools"] = {
        "enabled": True,
        "query": {"default_city": "济南市"},
        "amap": {"enabled": True, "secret_value": "test-only-key"},
    }
    draft = await store.create_draft(HubConfig.model_validate(candidate), actor="test")
    await store.publish(draft.version, actor="test")
    backend = TextToolCallRouter()
    monkeypatch.setattr(
        "app.chat.service.build_query_tool_runtime",
        lambda config, secrets: FakeToolRuntime(),
    )
    service = ChatService(database, store, router_builder=lambda config: backend)
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="text tool")
    deltas: list[str] = []
    tool_events: list[dict[str, object]] = []

    async def capture_tool_event(event: dict[str, object]) -> None:
        tool_events.append(event)

    pending = await service.start_turn(
        conversation.id,
        user_id=user.id,
        text="济南今天天气怎么样？",
        privacy_level=PrivacyLevel.L1,
    )
    result = await service.run_stream(
        pending,
        _append_chat_delta(deltas),
        capture_tool_event,
    )

    # 正文标签既不流出给前端，也不沉淀进消息历史
    assert deltas == ["济南现在多云，29℃。"]
    assert "<get_weather>" not in result.assistant_message.content
    assert [event["type"] for event in tool_events] == ["tool.started", "tool.finished"]
    assert tool_events[1]["tool"] == "get_weather"
    assert tool_events[1]["ok"] is True
    # 工具真的执行了：follow-up 请求回注了还原出的原生调用与结果
    assert len(backend.requests) == 2
    followup = backend.requests[1]
    assert followup.tools == []
    assert followup.tool_choice == "none"
    replayed_call = followup.messages[-2]
    assert replayed_call.role == "assistant"
    assert [call.function.name for call in replayed_call.tool_calls] == ["get_weather"]
    assert followup.messages[-1].role == "tool"
    meta = result.assistant_message.decision_meta or {}
    calls = meta["tool_calls"]
    assert isinstance(calls, list) and isinstance(calls[0], dict)
    assert calls[0]["tool_name"] == "get_weather"
    assert calls[0]["outcome"] == "success"

    # 非流式路径同样兜底
    turn = await service.send_message(
        conversation.id, user_id=user.id, text="再查一次", privacy_level=PrivacyLevel.L1
    )
    assert "<get_weather>" not in turn.assistant_message.content
    meta = turn.assistant_message.decision_meta or {}
    calls = meta["tool_calls"]
    assert isinstance(calls, list) and isinstance(calls[0], dict)
    assert calls[0]["tool_name"] == "get_weather"


class LoopingToolRouter:
    """按脚本回放多轮补全：带工具目录时依次返回 tool_sequence 里的调用，
    目录仍在但脚本耗尽、或目录被移除时返回终答。"""

    def __init__(self, *, tool_sequence: list[str], final_text: str) -> None:
        self.tool_sequence = list(tool_sequence)
        self.final_text = final_text
        self.requests: list[CompletionRequest] = []
        self._call_index = 0

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        if request.tools and self._call_index < len(self.tool_sequence):
            name = self.tool_sequence[self._call_index]
            self._call_index += 1
            return CompletionResult(
                text=f"我先查{name}。",
                provider="openai_compatible",
                model="tool-model",
                endpoint="cloud",
                route=request.route,
                finish_reason="tool_calls",
                latency_ms=10,
                tool_calls=[
                    ToolCall(
                        id=f"call-{self._call_index}",
                        function={"name": name, "arguments": {"location": "济南市"}},
                    )
                ],
            )
        return CompletionResult(
            text=self.final_text,
            provider="openai_compatible",
            model="tool-model",
            endpoint="cloud",
            route=request.route,
            finish_reason="stop",
            latency_ms=8,
        )

    async def stream(
        self,
        request: CompletionRequest,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> CompletionResult:
        result = await self.complete(request)
        await on_delta(result.text)
        return result


def _loop_candidate(store: DatabaseConfigStore, max_tool_rounds: int) -> dict[str, object]:
    candidate = store.current.config.model_dump(mode="python")
    candidate["models"]["cloud"]["supports_tool_calling"] = True
    candidate["tools"] = {
        "enabled": True,
        "max_tool_rounds": max_tool_rounds,
        "query": {"default_city": "济南市"},
        "amap": {"enabled": True, "secret_value": "test-only-key"},
    }
    return candidate


async def _publish_loop_config(store: DatabaseConfigStore, *, max_tool_rounds: int) -> None:
    draft = await store.create_draft(
        HubConfig.model_validate(_loop_candidate(store, max_tool_rounds)), actor="test"
    )
    await store.publish(draft.version, actor="test")


async def test_tool_loop_chains_read_tools_across_rounds(
    database: Database,
    store: DatabaseConfigStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多轮循环：中间轮保留工具目录（tool_choice=auto），模型自然收笔后
    中间轮前导文本被丢弃、终答正常流出，全部轮次进 decision_meta。"""
    await _publish_loop_config(store, max_tool_rounds=3)
    backend = LoopingToolRouter(
        tool_sequence=["get_weather", "search_nearby"],
        final_text="济南多云 29 度，附近有超市。",
    )
    monkeypatch.setattr(
        "app.chat.service.build_query_tool_runtime",
        lambda config, secrets: FakeToolRuntime(),
    )
    service = ChatService(database, store, router_builder=lambda config: backend)
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="loop")
    deltas: list[str] = []
    tool_events: list[dict[str, object]] = []

    async def capture_tool_event(event: dict[str, object]) -> None:
        tool_events.append(event)

    pending = await service.start_turn(
        conversation.id,
        user_id=user.id,
        text="济南天气怎么样，附近有什么超市",
        privacy_level=PrivacyLevel.L1,
    )
    result = await service.run_stream(pending, _append_chat_delta(deltas), capture_tool_event)

    assert deltas == ["济南多云 29 度，附近有超市。"]
    assert [event["type"] for event in tool_events] == [
        "tool.started",
        "tool.finished",
        "tool.started",
        "tool.finished",
    ]
    assert len(backend.requests) == 3
    intermediate = backend.requests[1]
    assert intermediate.tools
    assert intermediate.tool_choice == "auto"
    assert intermediate.messages[-2].role == "assistant"
    assert intermediate.messages[-1].role == "tool"
    meta = result.assistant_message.decision_meta or {}
    calls = meta["tool_calls"]
    rounds = meta["tool_rounds"]
    assert isinstance(calls, list) and all(isinstance(call, dict) for call in calls)
    assert isinstance(rounds, list)
    assert [call["tool_name"] for call in calls] == ["get_weather", "search_nearby"]
    assert rounds == ["get_weather", "search_nearby"]


async def test_tool_loop_stops_at_configured_round_cap(
    database: Database,
    store: DatabaseConfigStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """轮次耗尽强制收束：最后一次跟进补全移除工具目录并 tool_choice=none，
    模型再想调用也只执行到配置的轮数。"""
    await _publish_loop_config(store, max_tool_rounds=2)
    backend = LoopingToolRouter(
        tool_sequence=["get_weather"] * 10,
        final_text="按你给的两条信息，济南今天多云。",
    )
    monkeypatch.setattr(
        "app.chat.service.build_query_tool_runtime",
        lambda config, secrets: FakeToolRuntime(),
    )
    service = ChatService(database, store, router_builder=lambda config: backend)
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="cap")

    turn = await service.send_message(
        conversation.id,
        user_id=user.id,
        text="济南天气怎么样",
        privacy_level=PrivacyLevel.L1,
    )

    assert turn.assistant_message.content == "按你给的两条信息，济南今天多云。"
    assert len(backend.requests) == 3
    final_request = backend.requests[2]
    assert final_request.tools == []
    assert final_request.tool_choice == "none"
    meta = turn.assistant_message.decision_meta or {}
    calls = meta["tool_calls"]
    rounds = meta["tool_rounds"]
    assert isinstance(calls, list) and all(isinstance(call, dict) for call in calls)
    assert isinstance(rounds, list)
    assert [call["tool_name"] for call in calls] == ["get_weather", "get_weather"]
    assert rounds == ["get_weather", "get_weather"]


class MultiDeltaRouter:
    """分多次 delta 输出，供取消信号在流中途触发的场景。"""

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        return CompletionResult(
            text="第一段第二段第三段",
            provider="openai_compatible",
            model="tool-model",
            endpoint="cloud",
            route=request.route,
            finish_reason="stop",
            latency_ms=5,
        )

    async def stream(
        self,
        request: CompletionRequest,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> CompletionResult:
        for chunk in ["第一段", "第二段", "第三段"]:
            await on_delta(chunk)
        return await self.complete(request)


async def test_cancel_turn_interrupts_stream_via_memory_signal(
    database: Database,
    store: DatabaseConfigStore,
) -> None:
    """PERE-02：取消后流式热路径凭内存集合立即中断，不查库。"""
    service = ChatService(database, store, router_builder=lambda config: MultiDeltaRouter())
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="cancel")
    pending = await service.start_turn(
        conversation.id,
        user_id=user.id,
        text="讲三段话",
        privacy_level=PrivacyLevel.L1,
    )
    deltas: list[str] = []

    async def on_delta(delta: str) -> None:
        deltas.append(delta)
        if len(deltas) == 1:
            await service.cancel_turn(pending.generation_id, user_id=user.id)

    with pytest.raises(TurnCancelled):
        await service.run_stream(pending, on_delta)

    assert deltas == ["第一段"]
    # 回合收尾（失败路径）后取消标记逐出，不泄漏
    assert pending.generation_id not in service._cancelled_generations


async def test_published_database_config_is_used_on_next_turn(
    database: Database, store: DatabaseConfigStore
) -> None:
    seen_models: list[str] = []

    def builder(config: HubConfig) -> FakeRouter:
        seen_models.append(config.models["cloud"].model)
        return FakeRouter(config.models["cloud"].model, [])

    service = ChatService(database, store, router_builder=builder)
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title=None)
    await service.send_message(
        conversation.id, user_id=user.id, text="before", privacy_level=PrivacyLevel.L1
    )
    candidate = store.current.config.model_dump(mode="python")
    candidate["models"]["cloud"]["model"] = "dialogue-v2"
    draft = await store.create_draft(HubConfig.model_validate(candidate), actor="test")
    await store.publish(draft.version, actor="test")

    result = await service.send_message(
        conversation.id, user_id=user.id, text="after", privacy_level=PrivacyLevel.L1
    )

    assert seen_models == ["dialogue-v1", "dialogue-v2"]
    assert result.assistant_message.decision_meta is not None
    assert result.assistant_message.decision_meta["config_version"] == 2
    assert result.assistant_message.decision_meta["model"] == "dialogue-v2"


async def test_persona_published_by_another_store_is_used_on_next_turn(
    database: Database, store: DatabaseConfigStore
) -> None:
    requests: list[CompletionRequest] = []
    chat_personas = PersonaStore(database)
    admin_personas = PersonaStore(database)
    await chat_personas.load()
    await admin_personas.load()

    service = ChatService(
        database,
        store,
        router_builder=lambda config: FakeRouter(config.models["cloud"].model, requests),
        persona_store=chat_personas,
    )
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title=None)

    first = await service.send_message(
        conversation.id, user_id=user.id, text="before", privacy_level=PrivacyLevel.L1
    )
    assert first.assistant_message.decision_meta is not None
    assert first.assistant_message.decision_meta["persona_version"] == 1

    draft = await admin_personas.create_draft(
        PersonaConfig(
            name="Nova",
            identity="第二版人格",
            system_prompt="这是第二版核心要求",
            speaking_style="简洁直接",
            relationship="长期伙伴",
        ),
        actor="admin-worker",
    )
    published = await admin_personas.publish(draft.version)
    assert published.version == 2
    # Simulate another process: chat_personas still has its old in-memory v1.
    assert chat_personas.current.version == 1

    second = await service.send_message(
        conversation.id, user_id=user.id, text="after", privacy_level=PrivacyLevel.L1
    )

    assert second.assistant_message.decision_meta is not None
    assert second.assistant_message.decision_meta["persona_version"] == 2
    assert chat_personas.current.version == 2
    assert requests[-1].messages[0].role == "system"
    assert "你是 Nova：第二版人格。" in requests[-1].messages[0].content
    assert "核心要求：这是第二版核心要求" in requests[-1].messages[0].content


async def test_runtime_meta_and_rest_chat_share_published_persona_version(
    database: Database,
    store: DatabaseConfigStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[CompletionRequest] = []
    app = create_app(
        database,
        config_store=store,
        watch_config=False,
        admin_token="test-admin-token",
    )
    chat_service: ChatService = app.state.chat_service
    monkeypatch.setattr(
        chat_service,
        "_router_builder",
        lambda config: FakeRouter(config.models["cloud"].model, requests),
    )

    async with app.router.lifespan_context(app):
        persona_store: PersonaStore = app.state.persona_store
        draft = await persona_store.create_draft(
            PersonaConfig(
                name="Nova",
                identity="第二版人格",
                system_prompt="这是第二版核心要求",
                speaking_style="简洁直接",
                relationship="长期伙伴",
            ),
            actor="test",
        )
        published = await persona_store.publish(draft.version)
        avatar_store: AvatarStore = app.state.avatar_store
        avatar = await avatar_store.create_instance("light-core", "Nova Core")
        await avatar_store.bind_to_persona(published.version, avatar.id, is_default=True)
        auth_service: AuthService = app.state.auth_service
        auth_session = await auth_service.setup(
            display_name="Test",
            password="correct horse battery staple",
        )
        headers = {"Authorization": f"Bearer {auth_session.access_token}"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            runtime = await client.get("/api/v1/meta/runtime")
            avatar_asset = await client.get("/api/v1/avatar-assets/warm-daily/neutral.png")
            conversation = await client.post(
                "/api/v1/chat/conversations",
                headers=headers,
                json={"title": "persona contract"},
            )
            turn = await client.post(
                f"/api/v1/chat/conversations/{conversation.json()['id']}/messages",
                headers=headers,
                json={"text": "你好", "privacy_level": "L1"},
            )

    assert runtime.status_code == 200
    assert runtime.json()["persona"] == {
        "version": published.version,
        "content_hash": published.content_hash,
        "name": "Nova",
    }
    assert runtime.json()["avatar"] == {
        "instance_id": str(avatar.id),
        "pack_id": "light-core",
        "name": "Nova Core",
        "engine": "abstract",
        "customization": {},
        "assets": {},
    }
    assert avatar_asset.status_code == 200
    assert avatar_asset.headers["content-type"] == "image/png"
    assert conversation.status_code == 201
    assert turn.status_code == 200
    assistant = turn.json()["assistant_message"]
    assert assistant["decision_meta"]["persona_version"] == published.version
    assert assistant["decision_meta"]["avatar_instance_id"] == str(avatar.id)
    assert requests[0].messages[0].role == "system"
    assert "你是 Nova：第二版人格。" in requests[0].messages[0].content


class FailingRouter:
    async def complete(self, request: CompletionRequest) -> CompletionResult:
        del request
        raise LLMRouteExhausted("all_model_routes_failed")

    async def stream(
        self,
        request: CompletionRequest,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> CompletionResult:
        del on_delta
        return await self.complete(request)


async def test_model_failure_keeps_user_message_for_retry(
    database: Database, store: DatabaseConfigStore
) -> None:
    service = ChatService(database, store, router_builder=lambda config: FailingRouter())
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title=None)

    with pytest.raises(LLMRouteExhausted, match="all_model_routes_failed"):
        await service.send_message(
            conversation.id,
            user_id=user.id,
            text="keep me",
            privacy_level=PrivacyLevel.L1,
        )

    messages = await service.list_messages(conversation.id, user_id=user.id)
    assert [(message.seq, message.role, message.content) for message in messages] == [
        (1, "user", "keep me")
    ]


async def test_conversation_access_is_scoped_to_owning_user(
    database: Database, store: DatabaseConfigStore
) -> None:
    service = ChatService(
        database,
        store,
        router_builder=lambda config: FakeRouter(config.models["cloud"].model, []),
    )
    owner = await create_user(database, "Owner")
    stranger = await create_user(database, "Stranger")
    conversation = await service.create_conversation(user_id=owner.id, title="Private")

    assert await service.list_conversations(user_id=stranger.id) == []
    with pytest.raises(LookupError, match="conversation not found"):
        await service.list_messages(conversation.id, user_id=stranger.id)
    with pytest.raises(LookupError, match="conversation not found"):
        await service.send_message(
            conversation.id,
            user_id=stranger.id,
            text="unauthorized",
            privacy_level=PrivacyLevel.L1,
        )
    assert await service.list_messages(conversation.id, user_id=owner.id) == []


async def test_conversation_archive_preserves_messages_and_can_be_restored(
    database: Database, store: DatabaseConfigStore
) -> None:
    service = ChatService(database, store, router_builder=lambda config: FailingRouter())
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="保留")

    with pytest.raises(LLMRouteExhausted):
        await service.send_message(
            conversation.id,
            user_id=user.id,
            text="归档后仍然存在",
            privacy_level=PrivacyLevel.L1,
        )
    archived = await service.archive_conversation(conversation.id, user_id=user.id)

    assert archived.status == "archived"
    assert await service.list_conversations(user_id=user.id) == []
    assert [
        item.id for item in await service.list_conversations(user_id=user.id, status="archived")
    ] == [conversation.id]
    assert [
        item.content for item in await service.list_messages(conversation.id, user_id=user.id)
    ] == ["归档后仍然存在"]
    with pytest.raises(ValueError, match="conversation is archived"):
        await service.send_message(
            conversation.id,
            user_id=user.id,
            text="不能继续发送",
            privacy_level=PrivacyLevel.L1,
        )

    restored = await service.restore_conversation(conversation.id, user_id=user.id)

    assert restored.status == "active"
    assert [item.id for item in await service.list_conversations(user_id=user.id)] == [
        conversation.id
    ]


async def test_chat_api_auth_validation_and_stable_failure(
    database: Database, store: DatabaseConfigStore
) -> None:
    service = ChatService(database, store, router_builder=lambda config: FailingRouter())
    auth_service = AuthService(database)
    auth_session = await auth_service.setup(
        display_name="Test", password="correct horse battery staple"
    )
    app = FastAPI()
    app.include_router(create_chat_router(service, auth_service))
    headers = {"Authorization": f"Bearer {auth_session.access_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.get("/api/v1/chat/conversations")
        admin_unauthorized = await client.get(
            "/api/v1/chat/conversations",
            headers={"Authorization": "Bearer test-admin-token"},
        )
        created = await client.post(
            "/api/v1/chat/conversations",
            headers=headers,
            json={"title": "API test"},
        )
        conversation_id = created.json()["id"]
        l3 = await client.post(
            f"/api/v1/chat/conversations/{conversation_id}/messages",
            headers=headers,
            json={"text": "must not persist", "privacy_level": "L3"},
        )
        failure = await client.post(
            f"/api/v1/chat/conversations/{conversation_id}/messages",
            headers=headers,
            json={"text": "persist me", "privacy_level": "L1"},
        )
        messages = await client.get(
            f"/api/v1/chat/conversations/{conversation_id}/messages", headers=headers
        )
        messages_after = await client.get(
            f"/api/v1/chat/conversations/{conversation_id}/messages?after_seq=1",
            headers=headers,
        )

    assert unauthorized.status_code == 401
    assert admin_unauthorized.status_code == 401
    assert created.status_code == 201
    assert l3.status_code == 422
    assert failure.status_code == 503
    assert failure.json() == {"detail": {"reason_code": "all_model_routes_failed"}}
    assert [item["content"] for item in messages.json()] == ["persist me"]
    assert messages_after.status_code == 200
    assert messages_after.json() == []
    async with database.sessions() as session:
        count = await session.scalar(select(func.count()).select_from(MessageRecord))
    assert count == 1


async def test_chat_merges_profile_overrides_and_consolidates_in_background(
    database: Database, store: DatabaseConfigStore
) -> None:
    """用户告知的助手档案覆盖系统提示词；记忆沉淀后台完成不阻塞回复。"""

    from app.memory import (
        MemoryCandidate,
        MemoryOriginKind,
        MemoryStore,
        MemorySubjectKind,
        MemoryType,
    )

    memory_store = MemoryStore(database)
    requests: list[CompletionRequest] = []
    service = ChatService(
        database,
        store,
        router_builder=lambda config: FakeRouter(config.models["cloud"].model, requests),
        memory_store=memory_store,
    )
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="档案覆盖")
    await memory_store.add(
        MemoryCandidate(
            type=MemoryType.SEMANTIC,
            content="助手身高为 170 厘米",
            privacy_level=PrivacyLevel.L1,
            subject_kind=MemorySubjectKind.ASSISTANT,
            subject_key="assistant:primary",
            fact_key="profile.height",
            origin_kind=MemoryOriginKind.USER_STATEMENT,
        ),
        user_id=user.id,
    )

    await service.send_message(
        conversation.id, user_id=user.id, text="我喜欢吃火锅", privacy_level=PrivacyLevel.L1
    )

    assert "身高：170 厘米" in requests[0].messages[0].content
    await service.drain_background_work()
    stored = await memory_store.list_memories(user_id=user.id)
    assert any("火锅" in entry.content for entry in stored)


async def test_l2_assistant_profile_override_never_enters_public_prompt(
    database: Database, store: DatabaseConfigStore
) -> None:
    """L2 助手档案只进本地 L2 上下文，绝不随 L0/L1 系统提示出站。"""

    from app.memory import (
        MemoryCandidate,
        MemoryOriginKind,
        MemoryStore,
        MemorySubjectKind,
        MemoryType,
    )

    memory_store = MemoryStore(database)
    requests: list[CompletionRequest] = []
    service = ChatService(
        database,
        store,
        router_builder=lambda config: FakeRouter(config.models["cloud"].model, requests),
        memory_store=memory_store,
    )
    user = await create_user(database)
    public = await service.create_conversation(user_id=user.id, title="公开会话")
    private = await service.create_conversation(user_id=user.id, title="私密会话")
    await memory_store.add(
        MemoryCandidate(
            type=MemoryType.SEMANTIC,
            content="助手秘密代号为月见",
            privacy_level=PrivacyLevel.L2,
            subject_kind=MemorySubjectKind.ASSISTANT,
            subject_key="assistant:primary",
            fact_key="profile.secret_code",
            origin_kind=MemoryOriginKind.USER_STATEMENT,
        ),
        user_id=user.id,
    )

    await service.send_message(
        public.id, user_id=user.id, text="你好呀", privacy_level=PrivacyLevel.L1
    )
    await service.send_message(
        private.id, user_id=user.id, text="你好呀", privacy_level=PrivacyLevel.L2
    )

    assert "月见" not in requests[0].messages[0].content
    assert "月见" in requests[1].messages[0].content


async def _summary_service(
    tmp_path: Path,
) -> tuple[ChatService, Database, list[CompletionRequest]]:
    """CTX 测试脚手架：文件库（并发会话各自连接），随测随建随关。"""
    requests: list[CompletionRequest] = []
    database = create_database(f"sqlite+aiosqlite:///{tmp_path}/ctx-summary.db")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    config_path = tmp_path / "hub.yaml"
    config_path.write_text(config_yaml(), encoding="utf-8")
    store = DatabaseConfigStore(database, config_path)
    await store.load()
    service = ChatService(
        database, store, router_builder=lambda config: FakeRouter("sum-model", requests)
    )
    return service, database, requests


async def _send_turns(
    service: ChatService, conversation_id: UUID, user_id: UUID, count: int
) -> None:
    for index in range(count):
        await service.send_message(
            conversation_id, user_id=user_id, text=f"消息{index}", privacy_level=PrivacyLevel.L1
        )
    await service.drain_background_work()


async def test_conversation_summary_updates_after_threshold(tmp_path: Path) -> None:
    """CTX：新消息累计到阈值后，后台把窗口内容压缩为滚动摘要。"""
    service, database, _ = await _summary_service(tmp_path)
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="ctx")
        await _send_turns(service, conversation.id, user.id, 6)

        async with database.sessions() as session:
            record = await session.get(ConversationRecord, conversation.id)
            assert record is not None
            assert record.summary_text == "reply from sum-model"
            # 触发阈值 10；任务可能在轮内任一时序调度（10/11/12 均合法），
            # 未覆盖的尾部由下一次触发补齐
            assert record.summary_until_seq is not None and record.summary_until_seq >= 10
    finally:
        await service.drain_background_work()
        await database.close()


async def test_conversation_summary_injected_beyond_context_window(tmp_path: Path) -> None:
    """CTX：对话超出 20 条窗口后，系统提示注入「此前对话要点」。"""
    service, database, requests = await _summary_service(tmp_path)
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="ctx-long")
        await _send_turns(service, conversation.id, user.id, 13)

        requests.clear()
        await service.send_message(
            conversation.id, user_id=user.id, text="最后一问", privacy_level=PrivacyLevel.L1
        )
        system_prompt = requests[0].messages[0].content
        assert "【此前对话要点" in system_prompt
        assert "reply from sum-model" in system_prompt
    finally:
        await service.drain_background_work()
        await database.close()


async def test_conversation_summary_not_injected_within_window(tmp_path: Path) -> None:
    """CTX：对话仍在 20 条窗口内时不注入摘要，避免与原文重复。"""
    service, database, requests = await _summary_service(tmp_path)
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="ctx-short")
        await _send_turns(service, conversation.id, user.id, 4)

        requests.clear()
        await service.send_message(
            conversation.id, user_id=user.id, text="再聊一句", privacy_level=PrivacyLevel.L1
        )
        assert "此前对话要点" not in requests[0].messages[0].content
    finally:
        await service.drain_background_work()
        await database.close()


async def test_transparency_report_renders_deterministically(
    database: Database,
    store: DatabaseConfigStore,
) -> None:
    """RPT：透明度问询不经模型，直接从认知决策记录渲染。"""
    requests: list[CompletionRequest] = []
    service = ChatService(database, store, router_builder=lambda config: FakeRouter("m", requests))
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="rpt")
    now = datetime.now(UTC)
    async with database.sessions.begin() as session:
        session.add(
            CognitiveDecisionRecord(
                id=uuid7(),
                user_id=user.id,
                event_id=uuid7(),
                trigger_kind="mail.received",
                decision="inform",
                reason_codes=["notable"],
                evidence_ids=[],
                confidence=0.8,
                urgency="normal",
                attention_score=0.7,
                policy_version="v1",
                created_at=now - timedelta(hours=1),
            )
        )
        session.add(
            CognitiveDecisionRecord(
                id=uuid7(),
                user_id=user.id,
                event_id=uuid7(),
                trigger_kind="browser.observed",
                decision="ignore",
                reason_codes=[],
                evidence_ids=[],
                confidence=0.6,
                urgency="low",
                attention_score=0.3,
                policy_version="v1",
                created_at=now - timedelta(hours=2),
            )
        )

    turn = await service.send_message(
        conversation.id,
        user_id=user.id,
        text="你今天主动做了什么？",
        privacy_level=PrivacyLevel.L1,
    )

    content = turn.assistant_message.content
    assert "主动开口 1 次" in content
    assert "mail.received" in content
    assert "1 件事我注意到但选择了保持安静" in content
    meta = turn.assistant_message.decision_meta or {}
    assert meta["provider"] == "hub"
    # 主链路没有经过模型：任何请求里都不该出现这条用户提问
    assert not any(
        "主动做了什么" in (message.content or "")
        for request in requests
        for message in request.messages
    )


class MultiCallRouter:
    """首个补全一次返回两个只读调用（或含写动作的混合），之后终答。"""

    def __init__(self, *, names: list[str]) -> None:
        self.names = list(names)
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        if request.tools and self.names:
            names, self.names = self.names, []
            return CompletionResult(
                text="我一次查完。",
                provider="openai_compatible",
                model="tool-model",
                endpoint="cloud",
                route=request.route,
                finish_reason="tool_calls",
                latency_ms=10,
                tool_calls=[
                    ToolCall(
                        id=f"call-{name}",
                        function={"name": name, "arguments": {"location": "济南市"}},
                    )
                    for name in names
                ],
            )
        return CompletionResult(
            text="查询完成：多云，附近有超市。",
            provider="openai_compatible",
            model="tool-model",
            endpoint="cloud",
            route=request.route,
            finish_reason="stop",
            latency_ms=8,
        )

    async def stream(
        self,
        request: CompletionRequest,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> CompletionResult:
        result = await self.complete(request)
        await on_delta(result.text)
        return result


async def test_multi_read_calls_execute_in_one_round(
    database: Database,
    store: DatabaseConfigStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BTL-02：模型并行调用两个只读工具时同轮全部执行并回填。"""
    await _publish_loop_config(store, max_tool_rounds=2)
    backend = MultiCallRouter(names=["get_weather", "search_nearby"])
    monkeypatch.setattr(
        "app.chat.service.build_query_tool_runtime",
        lambda config, secrets: FakeToolRuntime(),
    )
    service = ChatService(database, store, router_builder=lambda config: backend)
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="multi")

    turn = await service.send_message(
        conversation.id,
        user_id=user.id,
        text="济南天气和附近超市",
        privacy_level=PrivacyLevel.L1,
    )

    assert turn.assistant_message.content == "查询完成：多云，附近有超市。"
    followup = backend.requests[1]
    tool_messages = [m for m in followup.messages if m.role == "tool"]
    assert len(tool_messages) == 2
    assistant_call = next(m for m in followup.messages if m.role == "assistant")
    assert [c.function.name for c in assistant_call.tool_calls] == [
        "get_weather",
        "search_nearby",
    ]
    meta = turn.assistant_message.decision_meta or {}
    calls = meta["tool_calls"]
    assert isinstance(calls, list)
    assert [call["tool_name"] for call in calls] == ["get_weather", "search_nearby"]


async def test_mixed_write_calls_rejected_without_execution(
    database: Database,
    store: DatabaseConfigStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BTL-02：只读+写动作的组合拒绝执行，回错误让模型自我修正。"""
    await _publish_loop_config(store, max_tool_rounds=2)
    backend = MultiCallRouter(names=["get_weather", "home_control"])
    monkeypatch.setattr(
        "app.chat.service.build_query_tool_runtime",
        lambda config, secrets: FakeToolRuntime(),
    )
    service = ChatService(database, store, router_builder=lambda config: backend)
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title="mixed")

    turn = await service.send_message(
        conversation.id,
        user_id=user.id,
        text="查天气并开灯",
        privacy_level=PrivacyLevel.L1,
    )

    # 第一轮拒绝执行（multiple_tool_calls_not_allowed），模型收错后第二轮
    # 不再调用工具直接回答
    followup = backend.requests[1]
    tool_messages = [m for m in followup.messages if m.role == "tool"]
    assert len(tool_messages) == 1
    assert "multiple_tool_calls_not_allowed" in tool_messages[0].content
    meta = turn.assistant_message.decision_meta or {}
    calls = meta["tool_calls"]
    assert isinstance(calls, list) and len(calls) == 1
    assert calls[0]["outcome"] == "failed"


@pytest.mark.parametrize("scenario", ["correction", "unrelated_reply", "expired", "other_user"])
async def test_skill_learning_attributes_correction_to_previous_reply(
    database: Database,
    store: DatabaseConfigStore,
    scenario: str,
) -> None:
    from app.skills.learning import SkillRevisionLearner, TurnSkillRun

    observed: list[list[TurnSkillRun]] = []

    class LearnerSpy:
        has_correction = staticmethod(SkillRevisionLearner.has_correction)

        async def harvest(self, *, runs: list[TurnSkillRun], **kwargs: object) -> None:
            observed.append(runs)

    service = ChatService(
        database,
        store,
        router_builder=lambda config: FakeRouter("fake", []),
        skill_learner=cast(SkillRevisionLearner, LearnerSpy()),
    )
    user = await create_user(database)
    conversation = await service.create_conversation(user_id=user.id, title=None)
    first = await service.send_message(
        conversation.id,
        user_id=user.id,
        text="查询卡券",
        privacy_level=PrivacyLevel.L1,
    )
    await service.drain_background_work()
    async with database.sessions.begin() as session:
        record = await session.get(MessageRecord, first.assistant_message.id)
        assert record is not None
        record.decision_meta = {
            "tool_calls": [
                {
                    "tool_name": "skill.partner-coupons.coupon_list",
                    "outcome": "failed",
                    "reason_code": "connection_http_404",
                    "skill_version": 3,
                }
            ]
        }
        if scenario == "expired":
            record.created_at = datetime.now(UTC) - timedelta(minutes=31)
    if scenario == "unrelated_reply":
        await service.send_message(
            conversation.id,
            user_id=user.id,
            text="聊点别的",
            privacy_level=PrivacyLevel.L1,
        )
        await service.drain_background_work()
    if scenario == "other_user":
        user = await create_user(database, "Other")
        conversation = await service.create_conversation(user_id=user.id, title=None)
    await service.send_message(
        conversation.id,
        user_id=user.id,
        text="查询不对，接口改了",
        privacy_level=PrivacyLevel.L1,
    )
    await service.drain_background_work()
    assert len(observed) == 1
    expected = (
        [
            TurnSkillRun(
                "skill.partner-coupons.coupon_list",
                False,
                "connection_http_404",
                skill_version=3,
            )
        ]
        if scenario == "correction"
        else []
    )
    assert observed[0] == expected


async def test_lower_privacy_turn_excludes_private_history_and_summary(tmp_path: Path) -> None:
    service, database, _ = await _summary_service(tmp_path)
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="privacy")
        private = await service.start_turn(
            conversation.id,
            user_id=user.id,
            text="private-history-marker",
            privacy_level=PrivacyLevel.L2,
        )
        await service.cancel_turn(private.generation_id, user_id=user.id)
        async with database.sessions() as session, session.begin():
            record = await session.get(ConversationRecord, conversation.id)
            assert record is not None
            record.summary_text = "private-summary-marker"
            record.summary_until_seq = 1
        public = await service.start_turn(
            conversation.id,
            user_id=user.id,
            text="public question",
            privacy_level=PrivacyLevel.L1,
        )
        contents = " ".join(message.content for message in public.request.messages)
        assert "private-history-marker" not in contents
        assert "private-summary-marker" not in contents
        assert public.context_manifest[-1]["reason"] == "privacy_filtered"
        await service.cancel_turn(public.generation_id, user_id=user.id)
    finally:
        await service.drain_background_work()
        await database.close()


async def test_summary_refresh_inherits_privacy_of_previous_summary(tmp_path: Path) -> None:
    service, database, requests = await _summary_service(tmp_path)
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="summary privacy")
        async with database.sessions() as session, session.begin():
            record = await session.get(ConversationRecord, conversation.id)
            assert record is not None
            record.summary_text = "previous-private-summary"
            record.summary_until_seq = 1
            record.last_seq = 15
            for seq in range(1, 16):
                session.add(
                    MessageRecord(
                        id=uuid7(),
                        turn_id=uuid7(),
                        conversation_id=conversation.id,
                        seq=seq,
                        role="user",
                        content=f"message-{seq}",
                        privacy_level="L2" if seq == 1 else "L1",
                        created_at=datetime.now(UTC),
                    )
                )
        await service._update_conversation_summary(conversation.id)
        assert requests
        assert requests[0].privacy_level == PrivacyLevel.L2
        assert requests[0].route == LLMRoute.PRIVATE
        assert "previous-private-summary" in requests[0].messages[-1].content
    finally:
        await service.drain_background_work()
        await database.close()
