# ruff: noqa: RUF001
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.api import create_chat_router
from app.auth import AuthService
from app.avatar import AvatarStore
from app.chat import ChatService, RuntimeActionCapability
from app.cognition import (
    AttentionEngine,
    CognitiveCycle,
    CognitiveStore,
    RuleBasedDeliberator,
    WorldStateBuilder,
)
from app.config import DatabaseConfigStore, HubConfig
from app.db import AppUserRecord, Base, Database, MessageRecord, create_database
from app.home_assistant import HomeAssistantState, HomeGetStateTool
from app.ids import uuid7
from app.llm import (
    CompletionRequest,
    CompletionResult,
    LLMRoute,
    LLMRouteExhausted,
    ModelUsage,
    ToolCall,
)
from app.main import create_app
from app.persona import PersonaConfig, PersonaStore
from app.schemas import PrivacyLevel
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
    assert first.assistant_message.decision_meta == {
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
    )

    # 3 轮 send_message + 本条语音输入共 7 条历史; 语音窗口只保留最近 2 条
    assert [message.role for message in pending.request.messages] == [
        "system",
        "assistant",
        "user",
    ]
    assert pending.request.messages[-1].content == "语音输入"

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


async def test_l1_browser_tool_remains_unexposed(
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

    assert unauthorized.status_code == 401
    assert admin_unauthorized.status_code == 401
    assert created.status_code == 201
    assert l3.status_code == 422
    assert failure.status_code == 503
    assert failure.json() == {"detail": {"reason_code": "all_model_routes_failed"}}
    assert [item["content"] for item in messages.json()] == ["persist me"]
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
