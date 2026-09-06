from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, select, update

from app.cognition import CognitiveCycle, CognitiveDecision, SemanticEvent
from app.config import ConfigStore, DatabaseConfigStore, HubConfig
from app.db import (
    AppUserRecord,
    ConversationRecord,
    Database,
    InteractionTurnRecord,
    MessageRecord,
)
from app.ids import uuid7
from app.llm import CompletionRequest, CompletionResult, LLMMessage, LLMRoute, ToolCall
from app.llm.factory import build_router
from app.llm.provider import EnvSecretProvider
from app.memory import (
    DeletionReceipt,
    ExtractionBackend,
    MemoryExtractor,
    MemoryHit,
    MemoryIngester,
    MemoryRetriever,
    MemoryStatus,
    MemoryStore,
    MemorySubjectKind,
    RetrievalResult,
)
from app.persona import PersonaConfig, PersonaStore
from app.schemas import PrivacyLevel
from app.timeline import (
    HistoryRecallResult,
    HistoryRecallService,
    RecallMode,
    ScreenActivityRecallResult,
    ScreenActivityRecallService,
    TimelineStore,
    has_history_intent,
)
from app.tools import (
    ClientLocation,
    ToolContext,
    ToolExecution,
    ToolExecutor,
    ToolHandler,
    ToolRegistry,
    ToolResult,
    build_query_tool_runtime,
    nearby_tool_definition,
    route_tool_definition,
    select_device_tools,
    supports_device_capability,
    weather_tool_definition,
)

from .capabilities import (
    RuntimeActionCapability,
    RuntimeCapabilityProvider,
    render_reality_grounding,
)
from .memory_consistency import MemoryConsistencyGuard, MemoryConsistencyOutcome
from .reply import ControlStreamFilter, parse_agent_reply, structured_reply_instruction

MAX_CONTEXT_MESSAGES = 20

logger = logging.getLogger(__name__)


def _local_tool_model_ready(config: HubConfig) -> bool:
    private_route = config.routes[LLMRoute.PRIVATE]
    private_candidates = (private_route.primary, *private_route.fallbacks)
    return any(
        (candidate := config.models.get(name)) is not None
        and candidate.enabled
        and candidate.runs_local
        and candidate.supports_tool_calling
        and PrivacyLevel(candidate.max_privacy_level) is PrivacyLevel.L2
        for name in private_candidates
    )


def _cloud_tool_model_ready(config: HubConfig, llm_route: LLMRoute) -> bool:
    policy = config.routes.get(llm_route) or config.routes[LLMRoute.DIALOGUE]
    candidates = (policy.primary, *policy.fallbacks)
    return any(
        (candidate := config.models.get(name)) is not None
        and candidate.enabled
        and candidate.supports_tool_calling
        and PrivacyLevel(candidate.max_privacy_level) in {PrivacyLevel.L1, PrivacyLevel.L2}
        for name in candidates
    )


def _vision_ready(config: HubConfig, privacy_level: PrivacyLevel) -> bool:
    endpoint_name = config.capability_models.vision
    if endpoint_name is None:
        return False
    endpoint = config.models.get(endpoint_name)
    ready = bool(endpoint is not None and endpoint.enabled and endpoint.kind == "vision")
    if not ready or endpoint is None:
        return False
    if privacy_level is PrivacyLevel.L2:
        return bool(
            endpoint.runs_local and PrivacyLevel(endpoint.max_privacy_level) is PrivacyLevel.L2
        )
    return PrivacyLevel(endpoint.max_privacy_level) in {
        PrivacyLevel.L1,
        PrivacyLevel.L2,
    }


def _enabled_query_tools(
    config: HubConfig,
    privacy_level: PrivacyLevel,
    llm_route: LLMRoute,
) -> tuple[str, ...]:
    """查询类工具按配置开关与隐私等级挂载；具体调不调用由模型判断。"""
    if privacy_level not in {PrivacyLevel.L0, PrivacyLevel.L1}:
        return ()
    if not config.tools.enabled or not config.tools.query.enabled:
        return ()
    if not _cloud_tool_model_ready(config, llm_route):
        return ()
    query = config.tools.query
    selected: list[str] = []
    if query.weather_enabled:
        selected.append("get_weather")
    if query.nearby_enabled:
        selected.append("search_nearby")
    if query.route_enabled:
        selected.append("plan_route")
    return tuple(selected)


def _device_tool_ready(
    name: str,
    config: HubConfig,
    privacy_level: PrivacyLevel,
    llm_route: LLMRoute,
) -> bool:
    if name in {
        "desktop_open_app",
        "desktop_open_url",
        "desktop_set_volume",
        "desktop_clipboard_write",
    }:
        # PC-01 桌面动作：v1 只经 Action Registry 计划—确认—执行链路触发，
        # 不作为聊天工具直接暴露给模型（docs/39 J5「不通过通用命令提前开放」）。
        return False
    if name in {"reminder_create", "calendar_create", "contact_save", "contact_query"}:
        # 助手工具：写/读个人任务、日历与联系人库。L0 公开模式不读写个人数据，
        # L2 私密会话内容不入库（工具执行层同样兜底拒绝），仅 L1 开放。
        return privacy_level is PrivacyLevel.L1 and _cloud_tool_model_ready(config, llm_route)
    if name in {"mail_read", "mail_send"}:
        # 邮件是云端账号操作（读摘要/出站发送），L2 私密会话禁止外发，仅 L1 开放；
        # 未配置账号时工具自身 available=False。
        return privacy_level is PrivacyLevel.L1 and _cloud_tool_model_ready(config, llm_route)
    if name in {"pnkx_read_life", "pnkx_create_life"}:
        if privacy_level is PrivacyLevel.L1:
            return _cloud_tool_model_ready(config, llm_route)
        if privacy_level is PrivacyLevel.L2:
            return _local_tool_model_ready(config)
        return False
    if name in {"home_get_state", "home_get_history", "home_control"}:
        if not config.integrations.home_assistant.enabled:
            return False
        if privacy_level in {PrivacyLevel.L0, PrivacyLevel.L1}:
            return _cloud_tool_model_ready(config, llm_route)
        if privacy_level is PrivacyLevel.L2:
            return _local_tool_model_ready(config)
        return False
    if privacy_level is PrivacyLevel.L1:
        return bool(
            name == "capture_screen"
            and _cloud_tool_model_ready(config, llm_route)
            and _vision_ready(config, privacy_level)
        )
    if privacy_level is PrivacyLevel.L2:
        if not _local_tool_model_ready(config):
            return False
        if name == "capture_screen":
            return _vision_ready(config, privacy_level)
        return True
    return False


def render_time_context(now: datetime, timezone_name: str) -> str:
    """Render a trusted per-turn clock for the model in the user's timezone."""
    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        timezone_name = "UTC"
        timezone = ZoneInfo("UTC")
    local_now = now.astimezone(timezone)
    return (
        "【当前时间】\n"
        f"用户当地时间: {local_now.isoformat(timespec='seconds')}\n"
        f"用户时区: {timezone_name}\n"
        f"UTC 时间: {now.astimezone(UTC).isoformat(timespec='seconds')}\n"
        "以上是本条消息进入服务时的可信时间。涉及「现在几点」、日期、早晚或相对时间时, "
        "以此为准; 不要声称自己无法获知当前时间。"
    )


class CompletionBackend(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResult: ...

    async def stream(
        self, request: CompletionRequest, on_delta: Callable[[str], Awaitable[None]]
    ) -> CompletionResult: ...


@dataclass(frozen=True, slots=True)
class ConversationView:
    id: UUID
    user_id: UUID
    title: str | None
    status: str
    last_seq: int
    created_at: datetime
    last_active_at: datetime


@dataclass(frozen=True, slots=True)
class MessageView:
    id: UUID
    conversation_id: UUID
    turn_id: UUID
    seq: int
    role: str
    content: str
    privacy_level: str
    generation_id: UUID | None
    decision_meta: dict[str, object] | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ChatTurn:
    user_message: MessageView
    assistant_message: MessageView


@dataclass(frozen=True, slots=True)
class PendingTurn:
    turn_id: UUID
    generation_id: UUID
    conversation_id: UUID
    user_id: UUID
    turn_seq: int
    user_message: MessageView
    request: CompletionRequest
    config: HubConfig
    config_version: int
    persona: PersonaConfig
    persona_version: int
    memory_retrieval: RetrievalResult | None = None
    history_recall: HistoryRecallResult | None = None
    screen_activity_recall: ScreenActivityRecallResult | None = None
    runtime_capabilities: tuple[RuntimeActionCapability, ...] = ()
    tool_names: tuple[str, ...] = ()
    # 连接级临时位置(L2 原始信号): 仅随轮次存活于内存, 不写入任何持久化记录。
    client_location: ClientLocation | None = None
    cognitive_decision: CognitiveDecision | None = None


class TurnCancelled(RuntimeError):
    pass


class ChatService:
    def __init__(
        self,
        database: Database,
        config_store: ConfigStore | DatabaseConfigStore,
        *,
        router_builder: Callable[[HubConfig], CompletionBackend] | None = None,
        persona_store: PersonaStore | None = None,
        memory_store: MemoryStore | None = None,
        memory_extractor: MemoryExtractor | None = None,
        timeline_store: TimelineStore | None = None,
        history_recall_service: HistoryRecallService | None = None,
        screen_activity_recall_service: ScreenActivityRecallService | None = None,
        capability_provider: RuntimeCapabilityProvider | None = None,
        device_tool: ToolHandler | None = None,
        device_tools: Iterable[ToolHandler] = (),
        cognitive_cycle: CognitiveCycle | None = None,
        avatar_store: Any | None = None,
        goal_tracker: Any | None = None,
    ) -> None:
        self._database = database
        self._config_store = config_store
        self._persona_store = persona_store
        self._memory_store = memory_store
        self._timeline_store = timeline_store
        self._history_recall = history_recall_service or (
            HistoryRecallService(timeline_store) if timeline_store is not None else None
        )
        self._screen_activity_recall = screen_activity_recall_service or (
            ScreenActivityRecallService(timeline_store) if timeline_store is not None else None
        )
        self._capability_provider = capability_provider
        self._cognitive_cycle = cognitive_cycle
        self._avatar_store = avatar_store
        self._goal_tracker = goal_tracker
        handlers = list(device_tools)
        if device_tool is not None:
            handlers.append(device_tool)
        self._device_tools = ToolRegistry(handlers)
        self._memory_consistency_guard = MemoryConsistencyGuard()
        self._memory_retriever = MemoryRetriever(memory_store) if memory_store else None
        self._memory_ingester = (
            MemoryIngester(memory_store, extractor=memory_extractor) if memory_store else None
        )
        # 后台记忆任务的强引用集合：既防止任务被 GC，也支持停机前等待收尾
        self._background_tasks: set[asyncio.Task[None]] = set()
        secrets = EnvSecretProvider()
        self._router_builder = router_builder or (lambda config: build_router(config, secrets))

    async def create_conversation(
        self,
        *,
        user_id: UUID,
        title: str | None,
    ) -> ConversationView:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            user = await session.get(AppUserRecord, user_id)
            if user is None or user.status != "active":
                raise LookupError("active user not found")
            record = ConversationRecord(
                id=uuid7(),
                user_id=user_id,
                title=title,
                status="active",
                last_seq=0,
                last_turn_seq=0,
                created_at=now,
                last_active_at=now,
            )
            session.add(record)
        return self._conversation_view(record)

    async def list_conversations(self, *, user_id: UUID, limit: int = 50) -> list[ConversationView]:
        query = (
            select(ConversationRecord)
            .where(ConversationRecord.user_id == user_id)
            .order_by(ConversationRecord.last_active_at.desc())
            .limit(limit)
        )
        async with self._database.sessions() as session:
            records = list(await session.scalars(query))
        return [self._conversation_view(record) for record in records]

    async def list_messages(
        self, conversation_id: UUID, *, user_id: UUID, limit: int = 100
    ) -> list[MessageView]:
        async with self._database.sessions() as session:
            conversation = await session.get(ConversationRecord, conversation_id)
            if conversation is None or conversation.user_id != user_id:
                raise LookupError("conversation not found")
            records = list(
                await session.scalars(
                    select(MessageRecord)
                    .where(MessageRecord.conversation_id == conversation_id)
                    .order_by(MessageRecord.seq.desc())
                    .limit(limit)
                )
            )
        records.reverse()
        return [self._message_view(record) for record in records]

    async def send_message(
        self,
        conversation_id: UUID,
        *,
        user_id: UUID,
        text: str,
        privacy_level: PrivacyLevel,
        client_location: ClientLocation | None = None,
    ) -> ChatTurn:
        pending = await self.start_turn(
            conversation_id,
            user_id=user_id,
            text=text,
            privacy_level=privacy_level,
            client_location=client_location,
        )
        await self._transition(pending.turn_id, {"accepted"}, "thinking")
        try:
            backend = self._router_builder(pending.config)
            tool_executions: tuple[ToolExecution, ...] = ()
            consistency_request = pending.request
            deterministic_call = _deterministic_home_read_call(pending)
            if deterministic_call is not None:
                execution = await self._execute_tool_call(pending, [deterministic_call])
                tool_executions = (execution,)
                consistency_request = self._tool_result_request(
                    pending.request, deterministic_call, execution
                )
                result = await backend.complete(consistency_request)
            else:
                result = await backend.complete(pending.request)
            if deterministic_call is None and result.tool_calls:
                execution = await self._execute_tool_call(pending, result.tool_calls)
                tool_executions = (execution,)
                consistency_request = self._tool_followup_request(
                    pending.request, result, execution
                )
                direct_reply = _render_mail_send_receipt(execution.result)
                if direct_reply is not None:
                    result = result.model_copy(
                        update={
                            "text": direct_reply,
                            "tool_calls": [],
                            "finish_reason": "stop",
                        }
                    )
                else:
                    result = await backend.complete(consistency_request)
            consistency = await self._memory_consistency_guard.enforce(
                result=result,
                request=consistency_request,
                persona=pending.persona,
                retrieval=pending.memory_retrieval,
                backend=backend,
            )
            return await self._commit_turn(
                pending,
                consistency.result,
                backend=backend,
                consistency=consistency,
                tool_executions=tool_executions,
            )
        except BaseException:
            await self._fail_if_active(pending.turn_id)
            raise

    async def create_proactive_message(
        self,
        text: str,
        *,
        entity_id: str,
        rule_id: str,
        trigger_kind: str,
        privacy_level: PrivacyLevel = PrivacyLevel.L1,
        cognitive_decision: CognitiveDecision | None = None,
        target_user_id: UUID | None = None,
    ) -> tuple[UUID, MessageView] | None:
        """Persist a deterministic HA suggestion in the latest active conversation."""
        if privacy_level is PrivacyLevel.L3:
            return None
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            query = (
                select(ConversationRecord, AppUserRecord)
                .join(AppUserRecord, AppUserRecord.id == ConversationRecord.user_id)
                .where(
                    ConversationRecord.status == "active",
                    AppUserRecord.status == "active",
                )
            )
            if target_user_id is not None:
                query = query.where(ConversationRecord.user_id == target_user_id)
            row = (
                await session.execute(
                    query
                    .order_by(ConversationRecord.last_active_at.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                return None
            conversation, user = row
            conversation.last_seq += 1
            conversation.last_active_at = now
            turn_id = uuid7()
            record = MessageRecord(
                id=uuid7(),
                conversation_id=conversation.id,
                turn_id=turn_id,
                seq=conversation.last_seq,
                role="assistant",
                content=text,
                privacy_level=privacy_level.value,
                decision_meta={
                    "kind": "home_assistant_proactive",
                    "entity_id": entity_id,
                    "rule_id": rule_id,
                    "trigger_kind": trigger_kind,
                    **(
                        {"cognition": _cognitive_meta(cognitive_decision)}
                        if cognitive_decision is not None
                        else {}
                    ),
                    "agent_reply": {
                        "schema_version": 1,
                        "schema_ref": "aria.agent-reply/1",
                        "text": text,
                        "tts_text": text,
                        "emotion": "concerned",
                        "expressions": [],
                        "actions": [],
                        "parse_status": "structured",
                    },
                },
                created_at=now,
            )
            session.add(record)
        return user.id, self._message_view(record)

    async def start_turn(
        self,
        conversation_id: UUID,
        *,
        user_id: UUID,
        text: str,
        privacy_level: PrivacyLevel,
        max_context_messages: int | None = None,
        client_location: ClientLocation | None = None,
        llm_route: LLMRoute = LLMRoute.DIALOGUE,
    ) -> PendingTurn:
        if privacy_level is PrivacyLevel.L3:
            raise ValueError("L3 durable chat is not allowed")
        now = datetime.now(UTC)
        turn_id = uuid7()
        generation_id = uuid7()
        user_timezone = "Asia/Shanghai"
        async with self._database.sessions.begin() as session:
            user = await session.get(AppUserRecord, user_id)
            if user is None or user.status != "active":
                raise LookupError("active user not found")
            user_timezone = user.timezone
            conversation = await session.scalar(
                select(ConversationRecord)
                .where(ConversationRecord.id == conversation_id)
                .with_for_update()
            )
            if conversation is None or conversation.user_id != user_id:
                raise LookupError("conversation not found")
            if conversation.status != "active":
                raise ValueError("conversation is archived")
            conversation.last_seq += 1
            conversation.last_turn_seq += 1
            conversation.last_active_at = now
            user_record = MessageRecord(
                id=uuid7(),
                conversation_id=conversation_id,
                turn_id=turn_id,
                seq=conversation.last_seq,
                role="user",
                content=text,
                privacy_level=privacy_level.value,
                created_at=now,
            )
            session.add(user_record)
            await session.flush()
            session.add(
                InteractionTurnRecord(
                    id=turn_id,
                    conversation_id=conversation_id,
                    turn_seq=conversation.last_turn_seq,
                    generation_id=generation_id,
                    state="accepted",
                    state_version=1,
                    input_message_id=user_record.id,
                    created_at=now,
                )
            )

        history = await self._context_messages(
            conversation_id, limit=max_context_messages or MAX_CONTEXT_MESSAGES
        )
        cognitive_decision: CognitiveDecision | None = None
        if self._cognitive_cycle is not None:
            try:
                cognitive_decision = await self._cognitive_cycle.evaluate(
                    SemanticEvent(
                        event_id=turn_id,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        kind="user.message_received",
                        summary="用户发起了一次被动对话请求",
                        occurred_at=now,
                        privacy_level=privacy_level,
                        confidence=1,
                        evidence_ids=[str(user_record.id)],
                        attributes={"message_length": len(text)},
                        passive=True,
                    )
                )
            except Exception:
                logger.warning("passive cognitive cycle failed for turn %s", turn_id, exc_info=True)
        snapshot = (
            await self._config_store.refresh()
            if isinstance(self._config_store, DatabaseConfigStore)
            else self._config_store.current
        )
        persona_snapshot = await self._persona_store.refresh() if self._persona_store else None
        persona = persona_snapshot.persona if persona_snapshot else PersonaConfig()
        profile_overrides = await self._assistant_profile_overrides(
            user_id, turn_id=turn_id, privacy_level=privacy_level
        )
        runtime_capabilities: tuple[RuntimeActionCapability, ...] = ()
        if self._capability_provider is not None:
            try:
                runtime_capabilities = tuple(
                    await self._capability_provider.available_actions(user_id)
                )
            except Exception:
                # 设备能力查询失败不能影响聊天；失败时按“没有现实能力”收紧边界。
                logger.warning("runtime capability lookup failed", exc_info=True)
        pnkx_intent = _has_pnkx_intent(text)
        reality_block = render_reality_grounding(
            () if pnkx_intent else runtime_capabilities
        )
        time_block = render_time_context(now, user_timezone)
        history_intent = has_history_intent(text)
        memory_retrieval: RetrievalResult | None = None
        grounded_memory_hits: tuple[MemoryHit, ...] = ()
        memory_block = ""
        if self._memory_retriever is not None:
            memory_retrieval = await self._memory_retriever.retrieve(
                text, user_id=user_id, privacy_level=privacy_level
            )
            grounded_memory_hits = self._memory_retriever.grounded_hits(memory_retrieval)
            # Top-K 仍保留在 decision_meta 供调试，但只有证据级命中进入 Prompt。
            # 这样 importance/pin/subject bonus 只能给相关记忆排序，不能把弱近邻
            # 变成聊天中无缘无故冒出来的“记忆”。
            memory_block = MemoryRetriever.render_context(
                memory_retrieval,
                hits=grounded_memory_hits,
            )
        history_recall: HistoryRecallResult | None = None
        history_block = ""
        screen_activity_recall: ScreenActivityRecallResult | None = None
        screen_activity_block = ""
        if self._screen_activity_recall is not None:
            try:
                screen_activity_recall = await self._screen_activity_recall.recall(
                    text,
                    user_id=user_id,
                    privacy_level=privacy_level,
                    now=now,
                    timezone_name=user_timezone,
                )
                if screen_activity_recall is not None:
                    screen_activity_block = ScreenActivityRecallService.render_context(
                        screen_activity_recall,
                        timezone_name=user_timezone,
                    )
            except Exception:
                logger.warning(
                    "screen activity recall failed for turn %s", turn_id, exc_info=True
                )
        if (
            screen_activity_recall is None
            and self._history_recall is not None
            and history_intent
        ):
            try:
                history_recall = await self._history_recall.recall(
                    text,
                    user_id=user_id,
                    privacy_level=privacy_level,
                    now=now,
                    timezone_name=user_timezone,
                )
                if not (history_recall.mode is RecallMode.NONE and grounded_memory_hits):
                    history_block = HistoryRecallService.render_context(history_recall)
            except Exception:
                # 历史索引是增强路径，故障不能让普通聊天不可用。
                logger.warning("history recall failed for turn %s", turn_id, exc_info=True)
        query_tool_names = _enabled_query_tools(
            snapshot.config, privacy_level, llm_route
        )
        # 工具挂载只看在线能力与配置就绪；选哪个、何时调用由模型根据工具描述自行判断。
        candidate_device_tools = self._device_tools.names()
        if pnkx_intent:
            candidate_device_tools = tuple(
                name for name in candidate_device_tools if name.startswith("pnkx_")
            )
        device_tool_names = tuple(
            name
            for name in candidate_device_tools
            if supports_device_capability(
                name, (item.capability_id for item in runtime_capabilities)
            )
            and _device_tool_ready(name, snapshot.config, privacy_level, llm_route)
        )
        tool_names = (*query_tool_names, *device_tool_names)
        definition_builders = {
            "get_weather": weather_tool_definition,
            "search_nearby": nearby_tool_definition,
            "plan_route": route_tool_definition,
        }
        tool_definitions = [definition_builders[name]() for name in query_tool_names]
        tool_definitions.extend(self._device_tools.definitions(device_tool_names))
        request = CompletionRequest(
            trace_id=turn_id,
            messages=[
                LLMMessage(
                    role="system",
                    content=persona.render_system_prompt(profile_overrides=profile_overrides)
                    + structured_reply_instruction(persona)
                    + f"\n\n{time_block}"
                    + f"\n\n{reality_block}"
                    + (f"\n\n{memory_block}" if memory_block else "")
                    + (f"\n\n{screen_activity_block}" if screen_activity_block else "")
                    + (f"\n\n{history_block}" if history_block else ""),
                ),
                *[
                    LLMMessage(role=message.role, content=message.content)
                    for message in history
                    if message.role in {"user", "assistant"}
                ],
            ],
            privacy_level=privacy_level,
            route=llm_route,
            temperature=0.7,
            tools=tool_definitions,
        )
        return PendingTurn(
            turn_id=turn_id,
            generation_id=generation_id,
            conversation_id=conversation_id,
            user_id=user_id,
            turn_seq=conversation.last_turn_seq,
            user_message=self._message_view(user_record),
            request=request,
            config=snapshot.config,
            config_version=snapshot.version,
            persona=persona,
            persona_version=persona_snapshot.version if persona_snapshot else 0,
            memory_retrieval=memory_retrieval,
            history_recall=history_recall,
            screen_activity_recall=screen_activity_recall,
            runtime_capabilities=runtime_capabilities,
            tool_names=tool_names,
            client_location=client_location,
            cognitive_decision=cognitive_decision,
        )

    async def run_stream(
        self,
        pending: PendingTurn,
        on_delta: Callable[[str], Awaitable[None]],
        on_tool_event: Callable[[dict[str, object]], Awaitable[None]] | None = None,
    ) -> ChatTurn:
        await self._transition(pending.turn_id, {"accepted"}, "thinking")
        emitted = False
        stream_filter = ControlStreamFilter()
        buffer_for_consistency = self._memory_consistency_guard.requires_buffering(
            pending.memory_retrieval
        )

        async def guarded_delta(delta: str) -> None:
            nonlocal emitted
            if not emitted:
                await self._transition(pending.turn_id, {"thinking"}, "streaming")
                emitted = True
            elif not await self._turn_is_active(pending.turn_id):
                raise TurnCancelled("generation_cancelled")
            await on_delta(delta)

        async def filtered_delta(delta: str) -> None:
            nonlocal emitted
            for visible in stream_filter.feed(delta):
                if buffer_for_consistency:
                    if not emitted:
                        await self._transition(pending.turn_id, {"thinking"}, "streaming")
                        emitted = True
                    elif not await self._turn_is_active(pending.turn_id):
                        raise TurnCancelled("generation_cancelled")
                else:
                    await guarded_delta(visible)

        try:
            backend = self._router_builder(pending.config)
            consistency_request = pending.request
            tool_executions: tuple[ToolExecution, ...] = ()
            deterministic_call = _deterministic_home_read_call(pending)
            if deterministic_call is not None:
                if on_tool_event is not None:
                    await on_tool_event(
                        {
                            "type": "tool.started",
                            "tool": deterministic_call.function.name,
                            "label": _tool_label(deterministic_call.function.name),
                        }
                    )
                execution = await self._execute_tool_call(pending, [deterministic_call])
                if on_tool_event is not None:
                    await on_tool_event(
                        {
                            "type": "tool.finished",
                            "tool": execution.result.tool_name,
                            "ok": execution.result.ok,
                            "latency_ms": round(execution.result.latency_ms, 1),
                            "reason_code": execution.result.reason_code,
                        }
                    )
                tool_executions = (execution,)
                consistency_request = self._tool_result_request(
                    pending.request, deterministic_call, execution
                )
                result = await backend.stream(consistency_request, filtered_delta)
            elif pending.request.tools:
                initial_chunks: list[str] = []

                async def buffer_delta(delta: str) -> None:
                    if delta:
                        initial_chunks.append(delta)

                result = await backend.stream(pending.request, buffer_delta)
                if result.tool_calls:
                    if on_tool_event is not None:
                        await on_tool_event(
                            {
                                "type": "tool.started",
                                "tool": result.tool_calls[0].function.name,
                                "label": _tool_label(result.tool_calls[0].function.name),
                            }
                        )
                    execution = await self._execute_tool_call(pending, result.tool_calls)
                    if on_tool_event is not None:
                        await on_tool_event(
                            {
                                "type": "tool.finished",
                                "tool": execution.result.tool_name,
                                "ok": execution.result.ok,
                                "latency_ms": round(execution.result.latency_ms, 1),
                                "reason_code": execution.result.reason_code,
                            }
                        )
                    tool_executions = (execution,)
                    consistency_request = self._tool_followup_request(
                        pending.request, result, execution
                    )
                    if execution.result.tool_name.startswith("pnkx_"):
                        direct_reply = _render_pnkx_tool_reply(execution.result)
                        await filtered_delta(direct_reply)
                        result = result.model_copy(
                            update={
                                "text": direct_reply,
                                "tool_calls": [],
                                "finish_reason": "stop",
                            }
                        )
                    elif (mail_receipt := _render_mail_send_receipt(execution.result)) is not None:
                        await filtered_delta(mail_receipt)
                        result = result.model_copy(
                            update={
                                "text": mail_receipt,
                                "tool_calls": [],
                                "finish_reason": "stop",
                            }
                        )
                    else:
                        result = await backend.stream(consistency_request, filtered_delta)
                else:
                    for chunk in initial_chunks:
                        await filtered_delta(chunk)
            else:
                result = await backend.stream(pending.request, filtered_delta)
            for visible in stream_filter.finish():
                if buffer_for_consistency:
                    if not emitted:
                        await self._transition(pending.turn_id, {"thinking"}, "streaming")
                        emitted = True
                    elif not await self._turn_is_active(pending.turn_id):
                        raise TurnCancelled("generation_cancelled")
                else:
                    await guarded_delta(visible)
            consistency = await self._memory_consistency_guard.enforce(
                result=result,
                request=consistency_request,
                persona=pending.persona,
                retrieval=pending.memory_retrieval,
                backend=backend,
            )
            if buffer_for_consistency:
                # 未校验的 delta 已被上面的 filter 消费但没有发送给前端；这里只
                # 发布通过 Guard 的最终正文，避免错误事实先出现在页面再被修正。
                safe_text = parse_agent_reply(consistency.result.text, pending.persona).text
                await guarded_delta(safe_text)
            return await self._commit_turn(
                pending,
                consistency.result,
                backend=backend,
                consistency=consistency,
                tool_executions=tool_executions,
            )
        except BaseException:
            await self._fail_if_active(pending.turn_id)
            raise

    async def _execute_tool_call(
        self,
        pending: PendingTurn,
        calls: list[ToolCall],
    ) -> ToolExecution:
        if len(calls) != 1:
            call_id = calls[0].id if calls else "invalid-tool-call"
            tool_name = calls[0].function.name if calls else "invalid_tool_call"
            return ToolExecution(
                call_id=call_id,
                result=ToolResult(
                    ok=False,
                    tool_name=tool_name,
                    reason_code="multiple_tool_calls_not_allowed",
                    latency_ms=0,
                ),
            )
        call = calls[0]
        context = ToolContext(
            privacy_level=pending.request.privacy_level,
            user_id=pending.user_id,
            turn_id=pending.turn_id,
            user_text=pending.user_message.content,
            default_city=pending.config.tools.query.default_city,
            ephemeral_location=pending.client_location,
        )
        if self._device_tools.get(call.function.name) is not None:
            try:
                return await ToolExecutor(self._device_tools).execute(
                    call,
                    context,
                )
            except Exception:
                logger.warning(
                    "device tool runtime failed turn_id=%s tool=%s",
                    pending.turn_id,
                    call.function.name,
                    exc_info=True,
                )
                return ToolExecution(
                    call_id=call.id,
                    result=ToolResult(
                        ok=False,
                        tool_name=call.function.name,
                        reason_code="device_tool_unavailable",
                        latency_ms=0,
                    ),
                )
        runtime = None
        try:
            runtime = build_query_tool_runtime(pending.config, EnvSecretProvider())
            return await runtime.executor.execute(
                call,
                context,
            )
        except Exception:
            logger.warning(
                "query tool runtime failed turn_id=%s tool=%s",
                pending.turn_id,
                call.function.name,
                exc_info=True,
            )
            return ToolExecution(
                call_id=call.id,
                result=ToolResult(
                    ok=False,
                    tool_name=call.function.name,
                    reason_code="provider_unavailable",
                    latency_ms=0,
                ),
            )
        finally:
            if runtime is not None:
                await runtime.close()

    @staticmethod
    def _tool_followup_request(
        request: CompletionRequest,
        first_result: CompletionResult,
        execution: ToolExecution,
    ) -> CompletionRequest:
        selected_call = next(
            (call for call in first_result.tool_calls if call.id == execution.call_id),
            first_result.tool_calls[0],
        )
        return ChatService._tool_result_request(request, selected_call, execution)

    @staticmethod
    def _tool_result_request(
        request: CompletionRequest,
        selected_call: ToolCall,
        execution: ToolExecution,
    ) -> CompletionRequest:
        messages = [
            *request.messages,
            LLMMessage(role="assistant", content="", tool_calls=[selected_call]),
            LLMMessage(
                role="tool",
                tool_call_id=selected_call.id,
                name=selected_call.function.name,
                content=json.dumps(
                    execution.result.model_dump(mode="json"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        ]
        return CompletionRequest.model_validate(
            {
                **request.model_dump(mode="python"),
                "messages": messages,
                "tools": [],
                "tool_choice": "none",
            }
        )

    async def cancel_turn(
        self, generation_id: UUID, *, user_id: UUID, reason: str = "user_cancelled"
    ) -> bool:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            row = (
                await session.execute(
                    select(InteractionTurnRecord, ConversationRecord)
                    .join(
                        ConversationRecord,
                        ConversationRecord.id == InteractionTurnRecord.conversation_id,
                    )
                    .where(InteractionTurnRecord.generation_id == generation_id)
                    .with_for_update()
                )
            ).one_or_none()
            if row is None or row[1].user_id != user_id:
                raise LookupError("generation not found")
            turn = row[0]
            if turn.state in {"cancelled", "failed", "completed"}:
                return False
            turn.state = "cancelled"
            turn.state_version += 1
            turn.cancel_reason = reason
            turn.completed_at = now
        return True

    async def list_messages_after(
        self, conversation_id: UUID, *, user_id: UUID, after_seq: int, limit: int = 200
    ) -> list[MessageView]:
        async with self._database.sessions() as session:
            conversation = await session.get(ConversationRecord, conversation_id)
            if conversation is None or conversation.user_id != user_id:
                raise LookupError("conversation not found")
            records = list(
                await session.scalars(
                    select(MessageRecord)
                    .where(
                        MessageRecord.conversation_id == conversation_id,
                        MessageRecord.seq > after_seq,
                    )
                    .order_by(MessageRecord.seq)
                    .limit(limit)
                )
            )
        return [self._message_view(record) for record in records]

    async def delete_conversation(self, conversation_id: UUID, *, user_id: UUID) -> DeletionReceipt:
        """硬删除会话：连同回合、消息与沉淀记忆一起清除。

        先按消息来源清除记忆链，整个动作以 message/<会话ID> 实体记入
        删除台账；备份恢复后可按台账重放，被删内容不会复活。
        """
        async with self._database.sessions() as session:
            conversation = await session.get(ConversationRecord, conversation_id)
            if conversation is None or conversation.user_id != user_id:
                raise LookupError("conversation not found")
            message_ids = [
                str(row)
                for row in await session.scalars(
                    select(MessageRecord.id).where(MessageRecord.conversation_id == conversation_id)
                )
            ]
        receipt = DeletionReceipt(ledger_id=0, entity_id=str(conversation_id), deleted_ids=())
        if self._memory_store is not None:
            receipt = await self._memory_store.hard_delete_by_source(
                "message",
                message_ids,
                entity_kind="message",
                entity_id=str(conversation_id),
                actor="user",
                reason="conversation deleted by user",
                always_record=True,
            )
        if self._timeline_store is not None:
            # Timeline 是 Source 的派生索引，必须在原消息删除前清除，避免形成删除旁路。
            await self._timeline_store.purge_conversation(conversation_id)
        async with self._database.sessions.begin() as session:
            conversation = await session.scalar(
                select(ConversationRecord)
                .where(ConversationRecord.id == conversation_id)
                .with_for_update()
            )
            if conversation is None or conversation.user_id != user_id:
                raise LookupError("conversation not found")
            await session.execute(
                delete(InteractionTurnRecord).where(
                    InteractionTurnRecord.conversation_id == conversation_id
                )
            )
            await session.execute(
                delete(MessageRecord).where(MessageRecord.conversation_id == conversation_id)
            )
            await session.delete(conversation)
        return receipt

    async def recover_incomplete_turns(self) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            await session.execute(
                update(InteractionTurnRecord)
                .where(InteractionTurnRecord.state.in_({"accepted", "thinking", "streaming"}))
                .values(
                    state="cancelled",
                    state_version=InteractionTurnRecord.state_version + 1,
                    cancel_reason="process_restarted",
                    completed_at=now,
                )
            )

    async def _commit_turn(
        self,
        pending: PendingTurn,
        result: CompletionResult,
        *,
        backend: CompletionBackend | None = None,
        consistency: MemoryConsistencyOutcome | None = None,
        tool_executions: tuple[ToolExecution, ...] = (),
    ) -> ChatTurn:
        if consistency is None:
            consistency = await self._memory_consistency_guard.enforce(
                result=result,
                request=pending.request,
                persona=pending.persona,
                retrieval=pending.memory_retrieval,
                backend=backend,
            )
            result = consistency.result
        reply = parse_agent_reply(result.text, pending.persona)
        decision_meta: dict[str, object] = {
            "schema_version": 1,
            "config_version": pending.config_version,
            "persona_version": pending.persona_version,
            "agent_reply": reply.model_dump(mode="json"),
            "endpoint": result.endpoint,
            "provider": result.provider,
            "model": result.model,
            "route": result.route,
            "finish_reason": result.finish_reason,
            "usage": result.usage.model_dump(mode="json"),
            "latency_ms": result.latency_ms,
        }
        if self._avatar_store is not None and pending.persona_version is not None:
            try:
                default_avatar = await self._avatar_store.get_default_for_persona(
                    pending.persona_version
                )
                if default_avatar is not None:
                    decision_meta["avatar_instance_id"] = str(default_avatar.id)
                    decision_meta["avatar_pack_id"] = default_avatar.pack_id
            except Exception:
                logger.exception("failed to resolve default avatar for persona")
        if pending.cognitive_decision is not None:
            decision_meta["cognition"] = _cognitive_meta(pending.cognitive_decision)
        if tool_executions:
            decision_meta["tool_calls"] = [
                {
                    "call_id": execution.call_id,
                    "tool_name": execution.result.tool_name,
                    "latency_ms": execution.result.latency_ms,
                    "outcome": "success" if execution.result.ok else "failed",
                    "reason_code": execution.result.reason_code,
                    "provider": execution.result.provider,
                    "result_count": _tool_result_count(execution.result),
                    "cache_hit": execution.result.cache_hit,
                    # docs/35 §6.2: 只记解析来源枚举, 不记坐标或原始地址。
                    "location_source": execution.result.location_source,
                }
                for execution in tool_executions
            ]
            presentation = _tool_presentation(tool_executions[0].result)
            if presentation is not None:
                # 只落前端展示所需的公开字段，不含用户起点坐标或 provider 原始响应。
                decision_meta["tool_result"] = presentation
        grounded_memory_hits = (
            self._memory_retriever.grounded_hits(pending.memory_retrieval)
            if self._memory_retriever is not None and pending.memory_retrieval is not None
            else ()
        )
        grounded_memory_ids = {hit.memory.id for hit in grounded_memory_hits}
        if pending.memory_retrieval is not None:
            decision_meta["memory"] = {
                "policy_version": pending.memory_retrieval.policy_version,
                "candidate_count": pending.memory_retrieval.candidate_count,
                "consistency": {
                    "checked": consistency.checked,
                    "repaired": consistency.repaired,
                    "fallback_used": consistency.fallback_used,
                    "conflict_memory_ids": list(consistency.conflict_memory_ids),
                },
                "hits": [
                    {
                        "id": hit.memory.id,
                        "subject": hit.memory.subject_kind,
                        "subject_key": hit.memory.subject_key,
                        "fact_key": hit.memory.fact_key,
                        "type": hit.memory.type,
                        "score": round(hit.final_score, 4),
                        "reasons": list(hit.reasons),
                        "grounded": hit.memory.id in grounded_memory_ids,
                    }
                    for hit in pending.memory_retrieval.hits
                ],
            }
        if pending.screen_activity_recall is not None:
            screen_recall = pending.screen_activity_recall
            decision_meta["recall"] = {
                "mode": "screen_activity",
                "timeline_ids": [item.id for item in screen_recall.events],
                "source_ids": [item.source_id for item in screen_recall.events],
                "time_range": {
                    "start_at": screen_recall.temporal_range.start_at.isoformat(),
                    "end_at": screen_recall.temporal_range.end_at.isoformat(),
                },
                "candidate_count": screen_recall.candidate_count,
                "segment_count": len(screen_recall.segments),
                "truncated": screen_recall.truncated,
            }
        elif pending.history_recall is not None:
            recall_mode = pending.history_recall.mode.value
            if pending.history_recall.mode is RecallMode.NONE and grounded_memory_hits:
                recall_mode = RecallMode.MEMORY.value
            decision_meta["recall"] = {
                "mode": recall_mode,
                "timeline_ids": [item.id for item in pending.history_recall.events],
                "source_ids": [item.source_id for item in pending.history_recall.evidence],
                "time_range": {
                    "start_at": (
                        pending.history_recall.plan.start_at.isoformat()
                        if pending.history_recall.plan.start_at
                        else None
                    ),
                    "end_at": (
                        pending.history_recall.plan.end_at.isoformat()
                        if pending.history_recall.plan.end_at
                        else None
                    ),
                },
                "search_count": pending.history_recall.search_count,
                "candidate_count": pending.history_recall.candidate_count,
            }
        elif grounded_memory_hits:
            decision_meta["recall"] = {"mode": RecallMode.MEMORY.value}
        else:
            decision_meta["recall"] = {"mode": RecallMode.WORKING.value}
        decision_meta["runtime_capabilities"] = [
            item.capability_id for item in pending.runtime_capabilities
        ]
        assistant_time = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            conversation = await session.scalar(
                select(ConversationRecord)
                .where(ConversationRecord.id == pending.conversation_id)
                .with_for_update()
            )
            turn = await session.scalar(
                select(InteractionTurnRecord)
                .where(InteractionTurnRecord.id == pending.turn_id)
                .with_for_update()
            )
            if conversation is None or conversation.user_id != pending.user_id or turn is None:
                raise LookupError("conversation not found")
            if turn.state == "cancelled":
                raise TurnCancelled("generation_cancelled")
            if turn.state not in {"thinking", "streaming"}:
                raise RuntimeError("turn is not committable")
            conversation.last_seq += 1
            conversation.last_active_at = assistant_time
            assistant_record = MessageRecord(
                id=uuid7(),
                conversation_id=pending.conversation_id,
                turn_id=pending.turn_id,
                seq=conversation.last_seq,
                role="assistant",
                content=reply.text,
                privacy_level=str(pending.request.privacy_level),
                generation_id=pending.generation_id,
                decision_meta=decision_meta,
                created_at=assistant_time,
            )
            session.add(assistant_record)
            turn.state = "completed"
            turn.state_version += 1
            turn.completed_at = assistant_time
        turn_result = ChatTurn(
            user_message=pending.user_message,
            assistant_message=self._message_view(assistant_record),
        )
        await self._index_timeline(pending, assistant_message=turn_result.assistant_message)
        # 记忆沉淀含 LLM 提取，耗时不可控；转后台执行，绝不阻塞回复提交
        self._spawn_background(
            self._consolidate_memory(
                pending,
                assistant_message=turn_result.assistant_message,
                backend=backend,
            )
        )
        # 承诺识别同样走后台 utility 路由；失败静默不影响回合
        self._spawn_background(
            self._extract_commitments(
                pending,
                backend=backend,
            )
        )
        return turn_result

    async def _extract_commitments(
        self,
        pending: PendingTurn,
        *,
        backend: Any | None = None,
    ) -> None:
        if self._goal_tracker is None:
            return
        try:
            await self._goal_tracker.ingest_message(
                user_id=pending.user_id,
                message_id=pending.user_message.id,
                text=pending.user_message.content,
                privacy_level=pending.request.privacy_level,
                backend=backend,
            )
        except Exception:
            logger.warning(
                "commitment extraction failed for turn %s", pending.turn_id, exc_info=True
            )

    async def _index_timeline(
        self,
        pending: PendingTurn,
        *,
        assistant_message: MessageView,
    ) -> None:
        if self._timeline_store is None:
            return
        try:
            await self._timeline_store.index_completed_turn(
                user_id=pending.user_id,
                conversation_id=pending.conversation_id,
                user_message_id=pending.user_message.id,
                user_text=pending.user_message.content,
                user_occurred_at=pending.user_message.created_at,
                assistant_message_id=assistant_message.id,
                assistant_text=assistant_message.content,
                assistant_occurred_at=assistant_message.created_at,
                privacy_level=pending.request.privacy_level,
            )
        except Exception:
            logger.warning("timeline indexing failed for turn %s", pending.turn_id, exc_info=True)

    async def _assistant_profile_overrides(
        self, user_id: UUID, *, turn_id: UUID, privacy_level: PrivacyLevel
    ) -> dict[str, str]:
        """读取记忆库中用户明确告知的助手档案事实，按 fact_key 覆盖 Persona 基线。"""
        if self._memory_store is None:
            return {}
        try:
            slots = await self._memory_store.list_memories(
                user_id=user_id,
                subject_kind=MemorySubjectKind.ASSISTANT,
                subject_key="assistant:primary",
                status=MemoryStatus.ACTIVE,
                limit=32,
            )
        except Exception:
            # 档案查询失败只损失覆盖能力，回退 Persona 基线即可
            logger.warning("assistant profile lookup failed for turn %s", turn_id, exc_info=True)
            return {}
        # 隐私闸门：档案覆盖会随系统提示进入每次模型调用，
        # L2 助手事实只能进入强制本地的 L2 上下文，绝不随 L0/L1 云端出站
        allowed_levels = {"L0", "L1", "L2"} if privacy_level is PrivacyLevel.L2 else {"L0", "L1"}
        return {
            slot.fact_key: slot.content
            for slot in slots
            if slot.fact_key is not None and slot.privacy_level in allowed_levels
        }

    def _spawn_background(self, coroutine: Coroutine[None, None, None]) -> None:
        task = asyncio.create_task(coroutine, name="aria-memory-consolidation")
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def drain_background_work(self) -> None:
        """等待后台记忆任务完成；测试断言与优雅停机使用。"""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks)

    async def _consolidate_memory(
        self,
        pending: PendingTurn,
        *,
        assistant_message: MessageView,
        backend: ExtractionBackend | None = None,
    ) -> None:
        if self._memory_ingester is None:
            return
        try:
            retrieved_memories = (
                tuple(hit.memory for hit in pending.memory_retrieval.hits)
                if pending.memory_retrieval is not None
                else ()
            )
            await self._memory_ingester.ingest_turn(
                user_id=pending.user_id,
                user_message_id=pending.user_message.id,
                user_text=pending.user_message.content,
                user_occurred_at=pending.user_message.created_at,
                assistant_message_id=assistant_message.id,
                assistant_text=assistant_message.content,
                assistant_occurred_at=assistant_message.created_at,
                privacy_level=pending.request.privacy_level,
                retrieved_memories=retrieved_memories,
                backend=backend,
            )
        except Exception:
            # 已提交的回复绝不能因为记忆沉淀失败而失败；
            # 下一轮会基于自己的消息重新沉淀，这里只记日志
            logger.warning(
                "memory consolidation failed for turn %s", pending.turn_id, exc_info=True
            )

    async def _transition(self, turn_id: UUID, from_states: set[str], target: str) -> None:
        async with self._database.sessions.begin() as session:
            turn = await session.scalar(
                select(InteractionTurnRecord)
                .where(InteractionTurnRecord.id == turn_id)
                .with_for_update()
            )
            if turn is None:
                raise LookupError("turn not found")
            if turn.state == "cancelled":
                raise TurnCancelled("generation_cancelled")
            if turn.state == target:
                return
            if turn.state not in from_states:
                raise RuntimeError("invalid turn state transition")
            turn.state = target
            turn.state_version += 1

    async def _turn_is_active(self, turn_id: UUID) -> bool:
        async with self._database.sessions() as session:
            state = await session.scalar(
                select(InteractionTurnRecord.state).where(InteractionTurnRecord.id == turn_id)
            )
        return state in {"thinking", "streaming"}

    async def _fail_if_active(self, turn_id: UUID) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            turn = await session.scalar(
                select(InteractionTurnRecord)
                .where(InteractionTurnRecord.id == turn_id)
                .with_for_update()
            )
            if turn is not None and turn.state in {"accepted", "thinking", "streaming"}:
                turn.state = "failed"
                turn.state_version += 1
                turn.completed_at = now

    async def _context_messages(
        self, conversation_id: UUID, *, limit: int = MAX_CONTEXT_MESSAGES
    ) -> list[MessageRecord]:
        async with self._database.sessions() as session:
            records = list(
                await session.scalars(
                    select(MessageRecord)
                    .where(MessageRecord.conversation_id == conversation_id)
                    .order_by(MessageRecord.seq.desc())
                    .limit(limit)
                )
            )
        records.reverse()
        return records

    @staticmethod
    def _conversation_view(record: ConversationRecord) -> ConversationView:
        return ConversationView(
            id=record.id,
            user_id=record.user_id,
            title=record.title,
            status=record.status,
            last_seq=record.last_seq,
            created_at=_aware(record.created_at),
            last_active_at=_aware(record.last_active_at),
        )

    @staticmethod
    def _message_view(record: MessageRecord) -> MessageView:
        return MessageView(
            id=record.id,
            conversation_id=record.conversation_id,
            turn_id=record.turn_id,
            seq=record.seq,
            role=record.role,
            content=record.content,
            privacy_level=record.privacy_level,
            generation_id=record.generation_id,
            decision_meta=record.decision_meta,
            created_at=_aware(record.created_at),
        )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


_HOME_READ_FOLLOWUP_TERMS = (
    "然后呢",
    "查到了吗",
    "查到没",
    "有查到吗",
    "结果呢",
    "怎么样了",
)

_PNKX_INTENT_TERMS = (
    "pnkx",
    "待办",
    "菜谱",
    "餐食计划",
    "用餐计划",
    "购物清单",
    "订阅",
    "账本",
    "记账",
    "笔记",
    "日记",
    "纪念日",
)


def _has_pnkx_intent(text: str) -> bool:
    normalized = text.lower()
    return any(term in normalized for term in _PNKX_INTENT_TERMS)


def _deterministic_home_read_call(pending: PendingTurn) -> ToolCall | None:
    """Resolve unambiguous HA reads server-side instead of trusting model tool syntax."""
    capability_ids = tuple(item.capability_id for item in pending.runtime_capabilities)
    source_text = pending.user_message.content
    selected = select_device_tools(source_text, capability_ids)
    tool_name = selected[0] if selected else None
    if tool_name not in {"home_get_state", "home_get_history"} and any(
        term in source_text for term in _HOME_READ_FOLLOWUP_TERMS
    ):
        # A terse “查到了吗” should finish the immediately preceding device
        # lookup, even when the previous model failed to emit a real function call.
        for message in reversed(pending.request.messages[:-1]):
            if message.role != "user":
                continue
            previous = select_device_tools(message.content, capability_ids)
            if previous and previous[0] in {"home_get_state", "home_get_history"}:
                tool_name = previous[0]
                source_text = message.content
                break
    if tool_name not in {"home_get_state", "home_get_history"}:
        return None

    suffix = ":state.read" if tool_name == "home_get_state" else ":history.read"
    matches: dict[str, str] = {}
    for capability in pending.runtime_capabilities:
        if not capability.capability_id.endswith(suffix):
            continue
        entity_id = capability.capability_id.removeprefix("home_assistant:").removesuffix(suffix)
        label = (
            capability.label.removesuffix("历史")
            if tool_name == "home_get_history"
            else capability.label
        )
        if label in source_text or entity_id in source_text:
            matches[entity_id] = label
    if len(matches) != 1:
        return None
    target = next(iter(matches.values()))
    arguments: dict[str, object]
    if tool_name == "home_get_state":
        arguments = {"targets": [target]}
    else:
        hour_match = re.search(r"(\d{1,3})\s*小时", source_text)
        hours = min(int(hour_match.group(1)), 168) if hour_match else 24
        arguments = {
            "target": target,
            "hours": max(hours, 1),
            "limit": 50,
            "include_logbook": True,
        }
    return ToolCall(
        id=f"server-{uuid7()}",
        function={"name": tool_name, "arguments": arguments},
    )


def _cognitive_meta(decision: CognitiveDecision) -> dict[str, object]:
    """Persist auditable judgment metadata, never hidden chain-of-thought."""
    return {
        "decision_id": str(decision.id),
        "event_id": str(decision.event_id),
        "trigger_kind": decision.trigger_kind,
        "decision": str(decision.decision),
        "reason_codes": decision.reason_codes,
        "evidence_ids": decision.evidence_ids,
        "confidence": decision.confidence,
        "urgency": str(decision.urgency),
        "attention_score": decision.attention_score,
        "policy_version": decision.policy_version,
        "model_provider": decision.model_provider,
        "model_name": decision.model_name,
    }


def _tool_result_count(result: ToolResult) -> int:
    for key in (
        "results",
        "items",
        "labels",
        "forecast",
        "routes",
        "entities",
        "states",
        "logbook",
    ):
        value = result.data.get(key)
        if isinstance(value, list):
            return len(value)
    return 1 if result.ok else 0


_PNKX_RESOURCE_LABELS = {
    "cockpit": "生活概览",
    "reminders": "今日提醒",
    "notifications": "通知",
    "commemoration_days": "纪念日",
    "notes": "笔记",
    "note_folders": "笔记文件夹",
    "diaries": "日记",
    "subscriptions": "订阅",
    "subscription_forecast": "订阅预测",
    "shopping_lists": "购物清单",
    "shopping_items": "购物项",
    "recipes": "菜谱",
    "meal_plans": "餐食计划",
    "todos": "未完成待办",
    "todo_kanban": "待办看板",
    "todo_labels": "待办标签",
    "bookkeeping_accounts": "账本账户",
    "bookkeeping_classifications": "账目分类",
    "bookkeeping_records": "账目",
    "commemoration_day": "纪念日",
    "note": "笔记",
    "diary": "日记",
    "subscription": "订阅",
    "shopping_list": "购物清单",
    "shopping_item": "购物项",
    "recipe": "菜谱",
    "meal_plan": "餐食计划",
    "todo": "待办",
    "bookkeeping_record": "账目",
}


def _render_pnkx_tool_reply(result: ToolResult) -> str:
    """Render pnkx results locally so L2 data never needs a second model pass."""
    if not result.ok:
        return f"PNKX 操作没有完成（{result.reason_code or 'unknown_error'}）。"
    resource = str(result.data.get("resource") or "data")
    label = _PNKX_RESOURCE_LABELS.get(resource, "数据")
    if result.tool_name == "pnkx_create_life":
        return f"已在 PNKX 创建{label}。"

    items = result.data.get("items")
    if isinstance(items, list):
        total_value = result.data.get("total")
        total = total_value if isinstance(total_value, int) else len(items)
        if not items:
            return f"PNKX 中没有找到{label}。"
        lines = [f"PNKX 中共有 {total} 条{label}，前 {min(len(items), 5)} 条是："]
        lines.extend(
            f"{index}. {_pnkx_item_summary(item)}"
            for index, item in enumerate(items[:5], start=1)
        )
        return "\n".join(lines)

    labels = result.data.get("labels")
    if isinstance(labels, list):
        if not labels:
            return "PNKX 中还没有待办标签。"
        return "PNKX 中的待办标签：" + "、".join(str(item) for item in labels[:20])

    value = result.data.get("value")
    rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(rendered) > 2_000:
        rendered = rendered[:2_000] + "…"
    return f"PNKX {label}：{rendered}"


def _render_mail_send_receipt(result: ToolResult) -> str | None:
    """Render a confirmed SMTP receipt without another fallible model pass."""
    if result.tool_name != "mail_send" or not result.ok:
        return None
    duplicate = result.data.get("duplicate") is True
    sent = result.data.get("sent") is True
    if not sent and not duplicate:
        # The preview/confirmation phase still needs the model to restate the
        # recipient, subject and body for explicit user approval.
        return None
    recipients = result.data.get("recipients")
    recipient_text = ""
    if isinstance(recipients, list):
        rendered = [str(item).strip() for item in recipients if str(item).strip()]
        if rendered:
            recipient_text = f"收件人：{'、'.join(rendered)}。"
    if duplicate:
        return f"这封邮件已经发送成功，本次没有重复发送。{recipient_text}"
    return f"邮件已发送成功。{recipient_text}"


def _pnkx_item_summary(item: object) -> str:
    if not isinstance(item, dict):
        return str(item)
    primary = next(
        (
            str(item[key]).strip()
            for key in ("content", "title", "name", "label", "description")
            if item.get(key) not in (None, "")
        ),
        f"记录 {item.get('id', '')}".strip(),
    )
    details = [
        str(item[key])
        for key in (
            "planStartTime",
            "planDate",
            "date",
            "nextPaymentDate",
            "amount",
        )
        if item.get(key) not in (None, "")
    ]
    return " · ".join((primary, *details))


def _tool_presentation(result: ToolResult) -> dict[str, object] | None:
    data = result.data
    if not result.ok:
        candidates = data.get("candidates")
        if result.reason_code != "location_ambiguous" or not isinstance(candidates, list):
            return None
        return {
            "kind": "location_ambiguous",
            "tool_name": result.tool_name,
            "candidates": [
                {"name": str(item.get("name") or ""), "adcode": str(item.get("adcode") or "")}
                for item in candidates[:3]
                if isinstance(item, dict) and item.get("name")
            ],
        }
    common: dict[str, object] = {
        "provider": result.provider or "amap",
        "cache_hit": result.cache_hit,
    }
    if result.tool_name == "get_weather":
        return {
            **common,
            "kind": "weather",
            "fetched_at": data.get("fetched_at"),
            "report_time": data.get("report_time"),
            "location": data.get("resolved_location"),
            "current": data.get("current"),
            "forecast": data.get("forecast", []),
        }
    if result.tool_name == "search_nearby":
        results = data.get("results")
        return {
            **common,
            "kind": "nearby",
            "fetched_at": data.get("fetched_at"),
            "origin": data.get("resolved_origin"),
            "rank_by": data.get("rank_by"),
            "results": [
                {
                    key: item.get(key)
                    for key in (
                        "name",
                        "address",
                        "category",
                        "distance_m",
                        "duration_s",
                        "distance_basis",
                        "navigation_uri",
                    )
                }
                for item in (results if isinstance(results, list) else [])[:3]
                if isinstance(item, dict)
            ],
        }
    if result.tool_name == "plan_route":
        return {
            **common,
            "kind": "route",
            "fetched_at": data.get("fetched_at"),
            "origin": data.get("origin"),
            "destination": data.get("destination"),
            "mode": data.get("mode"),
            "distance_m": data.get("distance_m"),
            "duration_s": data.get("duration_s"),
            "steps": data.get("steps", []),
            "navigation_uri": data.get("navigation_uri"),
        }
    return None


def _tool_label(tool_name: str) -> str:
    return {
        "get_weather": "正在查询天气…",
        "search_nearby": "正在查找附近地点…",
        "plan_route": "正在规划路线…",
        "capture_screen": "正在读取电脑屏幕…",
        "inspect_webpage": "正在读取当前网页…",
        "home_get_state": "正在读取设备状态…",
        "home_get_history": "正在读取设备历史…",
        "home_control": "正在执行设备控制…",
        "pnkx_read_life": "正在读取 pnkx 生活数据…",
        "pnkx_create_life": "正在写入 pnkx 生活数据…",
        "reminder_create": "正在创建提醒…",
        "calendar_create": "正在创建日程…",
        "contact_save": "正在保存联系人…",
        "contact_query": "正在查找联系人…",
        "mail_read": "正在读取邮箱…",
        "mail_send": "正在发送邮件…",
    }.get(tool_name, "正在使用外部工具…")
