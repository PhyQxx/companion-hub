"""Start-turn lifecycle collaborator for ChatService: validate, prepare, assemble."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.cognition import CognitiveDecision, SemanticEvent, render_action_catalog
from app.config import ConfigSnapshot, DatabaseConfigStore, HubConfig
from app.db import (
    AppUserRecord,
    ConversationRecord,
    InteractionTurnRecord,
    MessageRecord,
    TaskRunRecord,
)
from app.harness.budget import BudgetDenied
from app.harness.context import ContextBlocks, ContextReference
from app.ids import uuid7
from app.integrations.mcp.chat_tools import McpReadToolHandler
from app.llm import CompletionRequest, LLMRoute
from app.memory import MemoryHit, MemoryRetriever, RetrievalResult
from app.persona import PersonaConfig
from app.runs.chat_parent import require_chat_parent, validate_parent
from app.runs.parent_budget import ParentBudgetScope
from app.runs.store import append_run_event
from app.schemas import PrivacyLevel
from app.skills.runtime import SkillReadToolHandler
from app.timeline import (
    BrowserActivityRecallResult,
    BrowserActivityRecallService,
    HistoryRecallResult,
    HistoryRecallService,
    RecallMode,
    ScreenActivityRecallResult,
    ScreenActivityRecallService,
    has_history_intent,
)
from app.tools import (
    ClientLocation,
    location_tool_definition,
    nearby_tool_definition,
    route_tool_definition,
    supports_device_capability,
    weather_tool_definition,
)

from .agent_loop import _aware
from .capabilities import (
    RuntimeActionCapability,
    render_reality_grounding,
)
from .context_sources import (
    attach_memory_lineage,
    memory_reference,
    timeline_reference,
)
from .models import PendingTurn
from .reply import structured_reply_instruction

if TYPE_CHECKING:
    from .service import ChatService

logger = logging.getLogger(__name__)

MAX_CONTEXT_MESSAGES = 20


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
    if query.location_enabled:
        selected.append("get_location")
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
        "browser_open_tab",
        "browser_form_read",
        "browser_form_fill",
        "browser_form_submit",
        "mcp_tool_call",
        "skill_write",
        "hermes_delegate",
    }:
        # PC-01/WEB-01/MCP-D/技能 S3/HRM：桌面、浏览器、外部 MCP、技能写与
        # hermes 委派只经 Action Registry 计划—确认—执行链路触发，不作为
        # 聊天工具直接暴露给模型。
        return False
    if name in {
        "reminder_create",
        "reminder_list",
        "reminder_cancel",
        "calendar_create",
        "contact_save",
        "contact_query",
        "workflow_save",
        "workflow_run",
        "propose_action",
        "commute_check",
        "focus_start",
        "focus_stop",
        "focus_status",
    }:
        # 助手工具：写/读个人任务、日历、联系人与流程库。L0 公开模式不读写
        # 个人数据，L2 私密会话内容不入库（工具执行层同样兜底拒绝），仅 L1 开放。
        return privacy_level is PrivacyLevel.L1 and _cloud_tool_model_ready(config, llm_route)
    if name in {"mail_read", "mail_send", "mail_sent", "delegate_task"}:
        # 邮件是云端账号操作（读摘要/出站发送/查本地发送日志），L2 私密会话
        # 禁止外发与留痕，仅 L1 开放；未配置账号时工具自身 available=False。
        return privacy_level is PrivacyLevel.L1 and _cloud_tool_model_ready(config, llm_route)
    if name in {
        "pnkx_read_life",
        "pnkx_create_life",
        "pnkx_update_life",
        "pnkx_delete_life",
    }:
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
    if name == "hermes_status":
        # HRM：hermes 服务器代理状态查询；出站只读 L1，未启用或私密会话不挂载
        if not config.hermes.enabled:
            return False
        return privacy_level is PrivacyLevel.L1 and _cloud_tool_model_ready(config, llm_route)
    if privacy_level is PrivacyLevel.L1:
        if name == "inspect_webpage":
            # 浏览器标签页读取在 L1 也开放；页面文本会进入本轮对话上下文，
            # 因此要求本轮路由模型支持工具调用（云端或本地由路由决定）。
            return _cloud_tool_model_ready(config, llm_route)
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
    "情侣卡券",
    "情侣券",
    "卡券",
)

_PNKX_CARD_TERMS = ("情侣卡券", "情侣券", "卡券")


def _has_pnkx_card_intent(text: str) -> bool:
    return any(term in text for term in _PNKX_CARD_TERMS)


def _has_pnkx_intent(text: str) -> bool:
    normalized = text.lower()
    return any(term in normalized for term in _PNKX_INTENT_TERMS)


@dataclass(slots=True)
class _PreparedTurn:
    """start_turn 事务阶段产物：回合记录已落库，上下文尚未装配。"""

    turn_id: UUID
    generation_id: UUID
    conversation_id: UUID
    user_id: UUID
    now: datetime
    user_record: MessageRecord
    last_turn_seq: int
    conversation_summary: tuple[str, int] | None
    summary_exclusion_reason: str
    summary_source_count: int
    summary_privacy: str
    user_timezone: str
    parent_budget_enabled: bool


@dataclass(slots=True)
class _AssembledTurn:
    """start_turn 上下文装配阶段产物：请求与挂载清单就绪，待构建 PendingTurn。"""

    request: CompletionRequest
    tool_names: tuple[str, ...]
    mcp_handlers: tuple[McpReadToolHandler, ...]
    skill_handlers: tuple[SkillReadToolHandler, ...]
    memory_retrieval: RetrievalResult | None
    history_recall: HistoryRecallResult | None
    screen_activity_recall: ScreenActivityRecallResult | None
    browser_activity_recall: BrowserActivityRecallResult | None
    runtime_capabilities: tuple[RuntimeActionCapability, ...]
    cognitive_decision: CognitiveDecision | None
    persona: PersonaConfig
    persona_version: int
    context_references: tuple[ContextReference, ...]
    context_manifest: tuple[dict[str, object], ...]


class TurnLifecycleHandlers:
    """start_turn 生命周期的协作者；经 service 薄包装保留既有调用语义。"""

    def __init__(self, service: ChatService) -> None:
        self._service = service

    async def start(
        self,
        conversation_id: UUID,
        *,
        user_id: UUID,
        text: str,
        privacy_level: PrivacyLevel,
        max_context_messages: int | None = None,
        client_location: ClientLocation | None = None,
        llm_route: LLMRoute = LLMRoute.DIALOGUE,
        client_request_id: str | None = None,
        parent_run_id: UUID | None = None,
        parent_budget_scope: dict[str, object] | None = None,
    ) -> PendingTurn:
        parent_budget_scope = self.validate_request(
            privacy_level=privacy_level,
            client_request_id=client_request_id,
            parent_run_id=parent_run_id,
            parent_budget_scope=parent_budget_scope,
        )
        await self.note_activity(text, user_id=user_id)
        service = self._service
        snapshot = (
            await service._config_store.refresh()
            if isinstance(service._config_store, DatabaseConfigStore)
            else service._config_store.current
        )
        prepared = await self.prepare(
            conversation_id=conversation_id,
            user_id=user_id,
            text=text,
            privacy_level=privacy_level,
            client_request_id=client_request_id,
            parent_run_id=parent_run_id,
            parent_budget_scope=parent_budget_scope,
            snapshot=snapshot,
        )
        assembled = await self.assemble(
            prepared,
            text=text,
            privacy_level=privacy_level,
            llm_route=llm_route,
            max_context_messages=max_context_messages,
            snapshot=snapshot,
        )
        return await self.finish(
            prepared,
            assembled,
            privacy_level=privacy_level,
            client_location=client_location,
            parent_run_id=parent_run_id,
            parent_budget_scope=parent_budget_scope,
            snapshot=snapshot,
        )

    def validate_request(
        self,
        *,
        privacy_level: PrivacyLevel,
        client_request_id: str | None,
        parent_run_id: UUID | None,
        parent_budget_scope: dict[str, object] | None,
    ) -> dict[str, object] | None:
        if parent_budget_scope is not None:
            if parent_run_id is None:
                raise BudgetDenied("chat_parent_scope_invalid")
            parent_budget_scope = ParentBudgetScope.read(parent_budget_scope).model_dump(
                mode="json"
            )
        if client_request_id is not None and not 1 <= len(client_request_id) <= 160:
            raise ValueError("invalid_client_request_id")
        if privacy_level is PrivacyLevel.L3:
            raise ValueError("L3 durable chat is not allowed")
        return parent_budget_scope

    async def note_activity(self, text: str, *, user_id: UUID) -> None:
        service = self._service
        if service._safety is not None:
            try:
                # SAFE-02：聊天确认意图（"知道了/已处理"）确认活跃安全告警
                acked = await service._safety.handle_user_text(text, user_id=user_id)
                if acked:
                    logger.info("safety alerts acknowledged via chat: %d", acked)
            except Exception:
                logger.warning("safety ack handling failed", exc_info=True)
        if service._activity_tracker is not None:
            # SAFE-01：聊天回合刷新最后活动时间
            service._activity_tracker.record(user_id)

    async def prepare(
        self,
        conversation_id: UUID,
        *,
        user_id: UUID,
        text: str,
        privacy_level: PrivacyLevel,
        client_request_id: str | None,
        parent_run_id: UUID | None,
        parent_budget_scope: dict[str, object] | None,
        snapshot: ConfigSnapshot,
    ) -> _PreparedTurn:
        now = datetime.now(UTC)
        turn_id = uuid7()
        generation_id = uuid7()
        user_timezone = "Asia/Shanghai"
        parent_budget_enabled = False
        try:
            async with self._service._database.sessions.begin() as session:
                parent = None
                if parent_run_id is not None:
                    parent = await require_chat_parent(
                        session,
                        parent_run_id,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        privacy_level=privacy_level,
                        lock=True,
                        quota_scope=parent_budget_scope,
                    )
                    parent_budget_enabled = parent_budget_scope is not None or bool(
                        parent.budget and parent.budget.get("enabled")
                    )
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
                if client_request_id is not None:
                    existing_run = await session.scalar(
                        select(TaskRunRecord.id).where(
                            TaskRunRecord.user_id == user_id,
                            TaskRunRecord.request_id == client_request_id,
                        )
                    )
                    if existing_run is not None:
                        raise ValueError("request_already_accepted")
                conversation.last_seq += 1
                conversation.last_turn_seq += 1
                conversation.last_active_at = now
                # CTX（docs/09 §6）：随事务快照会话滚动摘要，供系统提示注入
                conversation_summary = (
                    (conversation.summary_text, conversation.summary_until_seq)
                    if conversation.summary_text is not None
                    and conversation.summary_until_seq is not None
                    else None
                )
                summary_exclusion_reason = "outside_watermark_or_absent"
                summary_source_count = 0
                summary_privacy = privacy_level.value
                if conversation_summary is not None:
                    private_summary_source = await session.scalar(
                        select(MessageRecord.id)
                        .where(
                            MessageRecord.conversation_id == conversation_id,
                            MessageRecord.seq <= conversation_summary[1],
                            MessageRecord.privacy_level.not_in(
                                [
                                    level.value
                                    for level in PrivacyLevel
                                    if level.value <= privacy_level.value
                                ]
                            ),
                        )
                        .limit(1)
                    )
                    if private_summary_source is not None:
                        conversation_summary = None
                        summary_exclusion_reason = "privacy_filtered"
                if conversation_summary is not None:
                    summary_source_count, summary_privacy = (
                        await session.execute(
                            select(
                                func.count(MessageRecord.id),
                                func.max(MessageRecord.privacy_level),
                            ).where(
                                MessageRecord.conversation_id == conversation_id,
                                MessageRecord.seq <= conversation_summary[1],
                            )
                        )
                    ).one()
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
                run = TaskRunRecord(
                    id=turn_id,
                    request_id=client_request_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    parent_run_id=parent_run_id,
                    contract={
                        "schema_version": 1,
                        "kind": "chat.reply",
                        "input_message_id": str(user_record.id),
                        "criterion": "reply_committed",
                        "required_work": [],
                        **({"budget_parent_id": str(parent_run_id)} if parent_run_id else {}),
                        **({"quota_scope": parent_budget_scope} if parent_budget_scope else {}),
                    },
                    budget=snapshot.config.run_budget.model_dump(mode="json"),
                    deadline=now
                    + timedelta(seconds=snapshot.config.run_budget.interactive_deadline_seconds)
                    if snapshot.config.run_budget.enabled
                    else None,
                    status="accepted",
                    state_version=1,
                    event_seq=0,
                    cancel_epoch=0,
                    privacy_level=privacy_level.value,
                    created_at=now,
                    updated_at=now,
                )
                if parent is not None and parent.deadline is not None:
                    inherited_deadline = _aware(parent.deadline)
                    run.deadline = (
                        min(_aware(run.deadline), inherited_deadline)
                        if run.deadline is not None
                        else inherited_deadline
                    )
                session.add(run)
                await session.flush()
                await append_run_event(session, run, "run.accepted")
                session.add(
                    InteractionTurnRecord(
                        id=turn_id,
                        task_run_id=turn_id,
                        conversation_id=conversation_id,
                        turn_seq=conversation.last_turn_seq,
                        generation_id=generation_id,
                        state="accepted",
                        state_version=1,
                        input_message_id=user_record.id,
                        created_at=now,
                    )
                )
                if parent is not None:
                    await session.flush()
                    validate_parent(
                        parent,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        privacy_level=privacy_level,
                        quota_scope=parent_budget_scope,
                    )

        except IntegrityError as error:
            if "uq_task_run_user_request" in str(
                error.orig
            ) or "task_run.user_id, task_run.request_id" in str(error.orig):
                raise ValueError("request_idempotency_conflict") from error
            raise

        return _PreparedTurn(
            turn_id=turn_id,
            generation_id=generation_id,
            conversation_id=conversation_id,
            user_id=user_id,
            now=now,
            user_record=user_record,
            last_turn_seq=conversation.last_turn_seq,
            conversation_summary=conversation_summary,
            summary_exclusion_reason=summary_exclusion_reason,
            summary_source_count=summary_source_count,
            summary_privacy=summary_privacy,
            user_timezone=user_timezone,
            parent_budget_enabled=parent_budget_enabled,
        )

    async def assemble(
        self,
        prepared: _PreparedTurn,
        *,
        text: str,
        privacy_level: PrivacyLevel,
        llm_route: LLMRoute,
        max_context_messages: int | None,
        snapshot: ConfigSnapshot,
    ) -> _AssembledTurn:
        service = self._service
        history = await service._context_messages(
            prepared.conversation_id,
            limit=max_context_messages or MAX_CONTEXT_MESSAGES,
            privacy_level=privacy_level,
        )
        cognitive_decision: CognitiveDecision | None = None
        if service._cognitive_cycle is not None:
            try:
                cognitive_decision = await service._cognitive_cycle.evaluate(
                    SemanticEvent(
                        event_id=prepared.turn_id,
                        user_id=prepared.user_id,
                        conversation_id=prepared.conversation_id,
                        kind="user.message_received",
                        summary="用户发起了一次被动对话请求",
                        occurred_at=prepared.now,
                        privacy_level=privacy_level,
                        confidence=1,
                        evidence_ids=[str(prepared.user_record.id)],
                        attributes={"message_length": len(text)},
                        passive=True,
                    )
                )
            except Exception:
                logger.warning(
                    "passive cognitive cycle failed for turn %s", prepared.turn_id, exc_info=True
                )
        persona_snapshot = (
            await service._persona_store.refresh() if service._persona_store else None
        )
        persona = persona_snapshot.persona if persona_snapshot else PersonaConfig()
        profile_overrides, profile_references = await service._assistant_profile_overrides(
            prepared.user_id, turn_id=prepared.turn_id, privacy_level=privacy_level
        )
        runtime_capabilities: tuple[RuntimeActionCapability, ...] = ()
        if service._capability_provider is not None:
            try:
                runtime_capabilities = tuple(
                    await service._capability_provider.available_actions(prepared.user_id)
                )
            except Exception:
                # 设备能力查询失败不能影响聊天；失败时按“没有现实能力”收紧边界。
                logger.warning("runtime capability lookup failed", exc_info=True)
        pnkx_intent = _has_pnkx_intent(text)
        skill_handlers: tuple[SkillReadToolHandler, ...] = ()
        if service._skill_tools is not None and _cloud_tool_model_ready(snapshot.config, llm_route):
            try:
                skill_handlers = await service._skill_tools.select(
                    text, privacy_level=privacy_level
                )
            except Exception:
                logger.warning(
                    "skill selection failed for turn %s", prepared.turn_id, exc_info=True
                )
        # 工具挂载只看在线能力与配置就绪；选哪个、何时调用由模型根据工具描述自行判断。
        # 先于 reality_block 计算：能力边界提示需要知道本轮真正挂载了哪些工具。
        candidate_device_tools = service._device_tools.names()
        if pnkx_intent and any(name.startswith("pnkx_") for name in candidate_device_tools):
            # 仅当旧版 pnkx_* 聊天工具确实注册时才做「pnkx 意图独占」收缩；
            # 技能中心化后旧工具不再挂载，无条件过滤会把 propose_action、
            # reminder_* 等助手工具一并清空，日记/待办类消息将无任何工具可调。
            candidate_device_tools = tuple(
                name for name in candidate_device_tools if name.startswith("pnkx_")
            )
        if _has_pnkx_card_intent(text):
            # 情侣卡券走登录态 Bearer Skill；旧生活工具没有卡券资源。
            candidate_device_tools = tuple(
                name for name in candidate_device_tools if not name.startswith("pnkx_")
            )
        elif any(handler.name.startswith("skill.pnkx-") for handler in skill_handlers):
            # 已授权的 PNKX 只读 Skill 使用独立 Bearer 连接；不要同时提供旧令牌读取工具。
            candidate_device_tools = tuple(
                name for name in candidate_device_tools if name != "pnkx_read_life"
            )
        device_tool_names = tuple(
            name
            for name in candidate_device_tools
            if supports_device_capability(
                name, (item.capability_id for item in runtime_capabilities)
            )
            and _device_tool_ready(name, snapshot.config, privacy_level, llm_route)
        )
        reality_block = render_reality_grounding(
            () if pnkx_intent else runtime_capabilities,
            home_device_mode=snapshot.config.integrations.home_assistant.device_context_mode,
            mounted_device_tools=frozenset(device_tool_names),
            private_session_ready=_local_tool_model_ready(snapshot.config),
        )
        # 写动作目录仅 L1 渲染（A2 动作上限 L1；L0 不写个人数据，L2 私密不走云端）
        action_catalog_block = (
            render_action_catalog(service._action_registry)
            if service._action_registry is not None and privacy_level is PrivacyLevel.L1
            else ""
        )
        recent_device_block = service._recent_device_context(
            prepared.conversation_id,
            user_id=prepared.user_id,
            current_turn_seq=prepared.last_turn_seq,
            runtime_capabilities=runtime_capabilities,
            now=prepared.now,
        )
        time_block = render_time_context(prepared.now, prepared.user_timezone)
        history_intent = has_history_intent(text)
        memory_retrieval: RetrievalResult | None = None
        grounded_memory_hits: tuple[MemoryHit, ...] = ()
        memory_block = ""
        if service._memory_retriever is not None:
            memory_retrieval = await service._memory_retriever.retrieve(
                text, user_id=prepared.user_id, privacy_level=privacy_level
            )
            grounded_memory_hits = service._memory_retriever.grounded_hits(memory_retrieval)
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
        browser_activity_recall: BrowserActivityRecallResult | None = None
        browser_activity_block = ""
        if service._screen_activity_recall is not None:
            try:
                screen_activity_recall = await service._screen_activity_recall.recall(
                    text,
                    user_id=prepared.user_id,
                    privacy_level=privacy_level,
                    now=prepared.now,
                    timezone_name=prepared.user_timezone,
                )
                if screen_activity_recall is not None:
                    screen_activity_block = ScreenActivityRecallService.render_context(
                        screen_activity_recall,
                        timezone_name=prepared.user_timezone,
                    )
            except Exception:
                logger.warning(
                    "screen activity recall failed for turn %s", prepared.turn_id, exc_info=True
                )
        if service._browser_activity_recall is not None:
            try:
                browser_activity_recall = await service._browser_activity_recall.recall(
                    text,
                    user_id=prepared.user_id,
                    privacy_level=privacy_level,
                    now=prepared.now,
                    timezone_name=prepared.user_timezone,
                )
                if browser_activity_recall is not None:
                    browser_activity_block = BrowserActivityRecallService.render_context(
                        browser_activity_recall,
                        timezone_name=prepared.user_timezone,
                    )
            except Exception:
                logger.warning(
                    "browser activity recall failed for turn %s", prepared.turn_id, exc_info=True
                )
        if (
            screen_activity_recall is None
            and browser_activity_recall is None
            and service._history_recall is not None
            and history_intent
        ):
            try:
                history_recall = await service._history_recall.recall(
                    text,
                    user_id=prepared.user_id,
                    privacy_level=privacy_level,
                    now=prepared.now,
                    timezone_name=prepared.user_timezone,
                )
                if not (history_recall.mode is RecallMode.NONE and grounded_memory_hits):
                    history_block = HistoryRecallService.render_context(history_recall)
            except Exception:
                # 历史索引是增强路径，故障不能让普通聊天不可用。
                logger.warning("history recall failed for turn %s", prepared.turn_id, exc_info=True)
        query_tool_names = _enabled_query_tools(snapshot.config, privacy_level, llm_route)
        tool_names = (*query_tool_names, *device_tool_names)
        definition_builders = {
            "get_location": location_tool_definition,
            "get_weather": weather_tool_definition,
            "search_nearby": nearby_tool_definition,
            "plan_route": route_tool_definition,
        }
        tool_definitions = [definition_builders[name]() for name in query_tool_names]
        tool_definitions.extend(service._device_tools.definitions(device_tool_names))
        # MCP-C2：按本轮文本相关性挂载只读 MCP 工具（有界，L2 不挂）
        mcp_handlers: tuple[McpReadToolHandler, ...] = ()
        if service._mcp_tools is not None:
            try:
                mcp_handlers = service._mcp_tools.select(
                    text, config=snapshot.config, privacy_level=privacy_level
                )
            except Exception:
                logger.warning(
                    "mcp tool selection failed for turn %s", prepared.turn_id, exc_info=True
                )
        if mcp_handlers:
            tool_names = (*tool_names, *(handler.name for handler in mcp_handlers))
            tool_definitions.extend(handler.definition() for handler in mcp_handlers)
        skill_guidance = ""
        skill_guidance_references: tuple[ContextReference, ...] = ()
        if service._skill_tools is not None and privacy_level is not PrivacyLevel.L0:
            try:
                skill_guidance, guidance_skills = await service._skill_tools.guidance_snapshot(text)
                skill_guidance_references = tuple(
                    ContextReference(
                        kind="skill",
                        source_id=str(skill.id),
                        owner_id=str(prepared.user_id),
                        privacy_level="L1",
                        version=str(skill.version),
                    )
                    for skill in guidance_skills
                )
            except Exception:
                logger.warning(
                    "skill guidance selection failed for turn %s", prepared.turn_id, exc_info=True
                )
        if skill_handlers:
            tool_names = (*tool_names, *(handler.name for handler in skill_handlers))
            tool_definitions.extend(handler.definition() for handler in skill_handlers)
        if service._skill_drafts is not None and _cloud_tool_model_ready(
            snapshot.config, llm_route
        ):
            # propose_skill 只产出待审阅草稿，不外发数据；跟随工具模型可用性挂载
            tool_names = (*tool_names, service._skill_drafts.name)
            tool_definitions.append(service._skill_drafts.definition())
        if (
            service._web_fetch is not None
            and snapshot.config.tools.enabled
            and snapshot.config.tools.web_fetch.enabled
            and privacy_level in {PrivacyLevel.L0, PrivacyLevel.L1}
            and _cloud_tool_model_ready(snapshot.config, llm_route)
        ):
            # 只读网页抓取：仅 L0/L1，出站前做 SSRF 校验（工具内自守 L2）
            tool_names = (*tool_names, service._web_fetch.name)
            tool_definitions.append(service._web_fetch.definition())
        card_skill_unavailable = _has_pnkx_card_intent(text) and not skill_handlers
        blocks = ContextBlocks(
            time=time_block,
            reality=reality_block,
            action_catalog=action_catalog_block,
            recent_device=recent_device_block,
            memory=memory_block,
            screen_activity=screen_activity_block,
            browser_activity=browser_activity_block,
            history_recall=history_block,
            skill_guidance=skill_guidance,
            unavailable_capability=(
                "【情侣卡券】本轮没有可用的卡券查询工具。请如实说明暂时无法读取卡券，"
                "需要在技能中心配置并启用对应的 Bearer API 连接，且使用 L1 会话；"
                "不得改用 PNKX 生活工具或猜测卡券数据。"
                if card_skill_unavailable
                else ""
            ),
        )
        request = CompletionRequest(
            trace_id=prepared.turn_id,
            messages=service._context_assembler.assemble(
                system_prompt=persona.render_system_prompt(profile_overrides=profile_overrides),
                reply_instruction=structured_reply_instruction(persona),
                history=history,
                conversation_summary=prepared.conversation_summary,
                blocks=blocks,
            ),
            privacy_level=privacy_level,
            route=llm_route,
            temperature=0.7,
            tools=tool_definitions,
        )
        source_references: dict[str, tuple[ContextReference, ...]] = {
            "skill_guidance": skill_guidance_references,
            "assistant_profile": profile_references,
            "history": tuple(
                ContextReference(
                    kind="message",
                    source_id=str(message.id),
                    owner_id=str(prepared.user_id),
                    privacy_level=message.privacy_level,
                    version=str(message.seq),
                    included=message.role in {"user", "assistant"},
                    reason="history" if message.role in {"user", "assistant"} else "role_filtered",
                )
                for message in history
            ),
            "memory": tuple(
                memory_reference(hit, included=hit in grounded_memory_hits)
                for hit in memory_retrieval.hits
            )
            if memory_retrieval
            else (),
            "screen_activity": tuple(
                timeline_reference(event) for event in screen_activity_recall.events
            )
            if screen_activity_recall
            else (),
            "browser_activity": tuple(
                timeline_reference(event) for event in browser_activity_recall.events
            )
            if browser_activity_recall
            else (),
            "history_recall": tuple(timeline_reference(event) for event in history_recall.events)
            if history_recall and history_block
            else (),
        }
        async with service._database.sessions() as session:
            for group in ("memory", "assistant_profile"):
                source_references[group] = await attach_memory_lineage(
                    session,
                    source_references[group],
                    owner_id=prepared.user_id,
                )
        if (
            service._context_assembler.summary_block(history, prepared.conversation_summary)
            and prepared.conversation_summary
        ):
            source_references["conversation_summary"] = (
                ContextReference(
                    kind="summary",
                    source_id=str(prepared.conversation_id),
                    owner_id=str(prepared.user_id),
                    privacy_level=prepared.summary_privacy or privacy_level.value,
                    version=str(prepared.conversation_summary[1]),
                    source_count=prepared.summary_source_count,
                ),
            )
        request = request.model_copy(
            update={
                "context_parts": service._context_assembler.context_parts(
                    system_prompt=persona.render_system_prompt(profile_overrides=profile_overrides),
                    reply_instruction=structured_reply_instruction(persona),
                    blocks=blocks,
                    history=history,
                    conversation_summary=prepared.conversation_summary,
                )
            }
        )
        return _AssembledTurn(
            request=request,
            tool_names=tool_names,
            mcp_handlers=mcp_handlers,
            skill_handlers=skill_handlers,
            memory_retrieval=memory_retrieval,
            history_recall=history_recall,
            screen_activity_recall=screen_activity_recall,
            browser_activity_recall=browser_activity_recall,
            runtime_capabilities=runtime_capabilities,
            cognitive_decision=cognitive_decision,
            persona=persona,
            persona_version=persona_snapshot.version if persona_snapshot else 0,
            context_references=tuple(ref for group in source_references.values() for ref in group),
            context_manifest=service._context_assembler.source_manifest(
                blocks=blocks,
                history=history,
                conversation_summary=prepared.conversation_summary,
                owner_id=str(prepared.user_id),
                privacy_level=privacy_level.value,
                summary_exclusion_reason=prepared.summary_exclusion_reason,
                references=source_references,
            ),
        )

    async def finish(
        self,
        prepared: _PreparedTurn,
        assembled: _AssembledTurn,
        *,
        privacy_level: PrivacyLevel,
        client_location: ClientLocation | None,
        parent_run_id: UUID | None,
        parent_budget_scope: dict[str, object] | None,
        snapshot: ConfigSnapshot,
    ) -> PendingTurn:
        service = self._service
        async with service._database.sessions.begin() as session:
            prepared_run = await session.get(TaskRunRecord, prepared.turn_id)
            if prepared_run is not None:
                prepared_run.config_version = snapshot.version
                prepared_run.persona_version = assembled.persona_version
        return PendingTurn(
            turn_id=prepared.turn_id,
            generation_id=prepared.generation_id,
            conversation_id=prepared.conversation_id,
            user_id=prepared.user_id,
            turn_seq=prepared.last_turn_seq,
            user_message=service._message_view(prepared.user_record),
            request=assembled.request,
            config=snapshot.config,
            config_version=snapshot.version,
            persona=assembled.persona,
            persona_version=assembled.persona_version,
            user_timezone=prepared.user_timezone,
            parent_run_id=parent_run_id,
            parent_budget_enabled=prepared.parent_budget_enabled,
            parent_budget_scope=parent_budget_scope,
            memory_retrieval=assembled.memory_retrieval,
            history_recall=assembled.history_recall,
            screen_activity_recall=assembled.screen_activity_recall,
            browser_activity_recall=assembled.browser_activity_recall,
            runtime_capabilities=assembled.runtime_capabilities,
            tool_names=assembled.tool_names,
            mcp_handlers=assembled.mcp_handlers,
            skill_handlers=assembled.skill_handlers,
            client_location=client_location,
            cognitive_decision=assembled.cognitive_decision,
            conversation_summary=prepared.conversation_summary,
            context_references=assembled.context_references,
            context_manifest=assembled.context_manifest,
        )
