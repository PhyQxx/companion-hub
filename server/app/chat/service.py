from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import case, select, update

from app.cognition import (
    ActionRegistry,
    CognitiveCycle,
    CognitiveDecision,
)
from app.config import ConfigStore, DatabaseConfigStore, HubConfig
from app.db import (
    AppUserRecord,
    ConversationRecord,
    Database,
    InteractionTurnRecord,
    JobRecord,
    MessageRecord,
    TaskRunRecord,
)
from app.db.claims import assert_current_claim
from app.db.deletions import purge_conversation
from app.harness.budget import BudgetDenied
from app.harness.context import ContextAssembler, ContextReference
from app.harness.joined_read import joined_read
from app.harness.loop import LoopOutcome
from app.ids import uuid7
from app.integrations.mcp.chat_tools import McpChatToolProvider
from app.llm import CompletionRequest, CompletionResult, LLMRoute, ToolCall
from app.llm.factory import CachedRouterBuilder
from app.llm.provider import EnvSecretProvider
from app.memory import (
    DeletionReceipt,
    ExtractionBackend,
    MemoryExtractor,
    MemoryIngester,
    MemoryRetriever,
    MemoryStatus,
    MemoryStore,
    MemorySubjectKind,
)
from app.persona import PersonaStore
from app.runs.budget import RunModelBudget, recover_stale_reservations
from app.runs.chat_parent import require_chat_parent
from app.runs.completion import recover_expired_model_runs
from app.runs.delivery import recover_expired_deliveries
from app.runs.model_accounting import DisabledModelAccounting
from app.runs.parent_budget import ParentBudgetScope
from app.runs.resources import recover_tool_reservations
from app.runs.speech_delivery import recover_expired_speech_deliveries
from app.runs.store import RunStore, transition_run
from app.schemas import PrivacyLevel
from app.skills.drafts import SkillDraftAssistant
from app.skills.learning import SkillRevisionLearner
from app.skills.runtime import SkillToolProvider
from app.timeline import (
    BrowserActivityRecallService,
    HistoryRecallService,
    ScreenActivityRecallService,
    TimelineStore,
)
from app.tools import (
    ClientLocation,
    FetchWebpageTool,
    ToolExecution,
    ToolHandler,
    ToolRegistry,
    select_device_tools,
)

from .agent_loop import AgentLoopHandlers, _aware, _tool_label
from .budget import BudgetedBackend
from .capabilities import (
    RuntimeActionCapability,
    RuntimeCapabilityProvider,
)
from .commit import CommitHandlers, _cognitive_meta
from .context_sources import (
    validate_references,
    version_stamp,
)
from .memory_consistency import MemoryConsistencyGuard, MemoryConsistencyOutcome
from .models import (
    ChatTurn,
    CompletionBackend,
    ConversationView,
    MessageView,
    PendingTurn,
    RecentDeviceReference,
    TurnCancelled,
)
from .postcommit import PostcommitWorker
from .postcommit_handlers import PostcommitHandlers
from .reply import ControlStreamFilter, parse_agent_reply
from .turn_lifecycle import MAX_CONTEXT_MESSAGES, TurnLifecycleHandlers

logger = logging.getLogger(__name__)


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
        browser_activity_recall_service: BrowserActivityRecallService | None = None,
        capability_provider: RuntimeCapabilityProvider | None = None,
        device_tool: ToolHandler | None = None,
        device_tools: Iterable[ToolHandler] = (),
        mcp_tools: McpChatToolProvider | None = None,
        skill_tools: SkillToolProvider | None = None,
        skill_drafts: SkillDraftAssistant | None = None,
        skill_learner: SkillRevisionLearner | None = None,
        web_fetch: FetchWebpageTool | None = None,
        safety: Any | None = None,
        cognitive_cycle: CognitiveCycle | None = None,
        avatar_store: Any | None = None,
        goal_tracker: Any | None = None,
        action_registry: ActionRegistry | None = None,
    ) -> None:
        self._database = database
        self.runs = RunStore(database)
        self._postcommit_handlers = PostcommitHandlers(self)
        self._agent_loop = AgentLoopHandlers(self)
        self._commit_handlers = CommitHandlers(self)
        self._turn_lifecycle = TurnLifecycleHandlers(self)
        self._postcommit = PostcommitWorker(database, self._run_postcommit)
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
        self._browser_activity_recall = browser_activity_recall_service or (
            BrowserActivityRecallService(timeline_store) if timeline_store is not None else None
        )
        self._capability_provider = capability_provider
        self._cognitive_cycle = cognitive_cycle
        # 自描述能力：注册表实时渲染进提示词，替代会漂移的手写能力文案
        self._action_registry = action_registry
        self._avatar_store = avatar_store
        self._goal_tracker = goal_tracker
        handlers = list(device_tools)
        if device_tool is not None:
            handlers.append(device_tool)
        self._device_tools = ToolRegistry(handlers)
        self._mcp_tools = mcp_tools
        self._skill_tools = skill_tools
        self._skill_drafts = skill_drafts
        self._skill_learner = skill_learner
        self._web_fetch = web_fetch
        # SAFE-02 SafetyAlertService：聊天确认意图入口（可选依赖，无则跳过）。
        # main.py 中投递服务晚于 ChatService 构造，装配后经 set_safety 注入。
        self._safety = safety
        # SAFE-01：聊天/语音回合即活动信号（装配后注入）
        self._activity_tracker = None
        self._context_assembler = ContextAssembler()
        self._memory_consistency_guard = MemoryConsistencyGuard()
        self._memory_retriever = MemoryRetriever(memory_store) if memory_store else None
        self._memory_ingester = (
            MemoryIngester(memory_store, extractor=memory_extractor) if memory_store else None
        )
        self._memory_review_worker = None
        if self._memory_ingester is not None:
            from app.skills.conversation_memory import ConversationMemoryTool
            from app.skills.conversation_memory_worker import ConversationMemoryWorker
            from app.skills.store import SkillStore

            review_tool = ConversationMemoryTool(self, SkillStore(database), self._memory_ingester)
            self._memory_review_worker = ConversationMemoryWorker(self, review_tool)
            self._device_tools.register(review_tool)
        # 后台记忆任务的强引用集合：既防止任务被 GC，也支持停机前等待收尾
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._memory_decay_task: asyncio.Task[None] | None = None
        self._recent_devices: dict[UUID, list[RecentDeviceReference]] = {}
        # PERE-02：本进程已取消的 generation_id（cancel_turn 写入，回合收尾
        # 逐出）。流式热路径的取消检查由此走内存而非逐 delta 查库。
        self._cancelled_generations: set[UUID] = set()
        # DELEG：取消回合时联动取消委派任务的回调（main 装配注入）
        self._deleg_canceller: Callable[[UUID], Awaitable[int]] | None = None
        secrets = EnvSecretProvider()
        # PERE-01：默认按配置指纹缓存 Router，省去每轮重建 provider；
        # 显式传入的 builder（测试假件等）不经过缓存。
        self._router_builder = router_builder or CachedRouterBuilder(secrets)

    def set_safety(self, safety: Any | None) -> None:
        """SAFE-02：投递服务晚于 ChatService 构造，装配后注入告警服务。"""
        self._safety = safety

    def set_deleg_canceller(self, canceller: Callable[[UUID], Awaitable[int]] | None) -> None:
        """DELEG：取消回合时联动取消委派任务的回调（main 装配注入）。"""
        self._deleg_canceller = canceller

    def set_activity_tracker(self, tracker: Any | None) -> None:
        """SAFE-01：聊天回合即活动信号，装配后注入追踪器。"""
        self._activity_tracker = tracker

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

    async def list_conversations(
        self, *, user_id: UUID, limit: int = 50, status: str = "active"
    ) -> list[ConversationView]:
        if status not in {"active", "archived"}:
            raise ValueError("unsupported conversation status")
        query = (
            select(ConversationRecord)
            .where(
                ConversationRecord.user_id == user_id,
                ConversationRecord.status == status,
            )
            .order_by(ConversationRecord.last_active_at.desc())
            .limit(limit)
        )
        async with self._database.sessions() as session:
            records = list(await session.scalars(query))
        return [self._conversation_view(record) for record in records]

    async def archive_conversation(
        self, conversation_id: UUID, *, user_id: UUID
    ) -> ConversationView:
        return await self._set_conversation_status(
            conversation_id, user_id=user_id, status="archived"
        )

    async def restore_conversation(
        self, conversation_id: UUID, *, user_id: UUID
    ) -> ConversationView:
        return await self._set_conversation_status(
            conversation_id, user_id=user_id, status="active"
        )

    async def _set_conversation_status(
        self, conversation_id: UUID, *, user_id: UUID, status: str
    ) -> ConversationView:
        async with self._database.sessions.begin() as session:
            conversation = await session.scalar(
                select(ConversationRecord)
                .where(ConversationRecord.id == conversation_id)
                .with_for_update()
            )
            if conversation is None or conversation.user_id != user_id:
                raise LookupError("conversation not found")
            conversation.status = status
            await session.flush()
            view = self._conversation_view(conversation)
        return view

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
        await self._attach_memory_receipts(records, user_id)
        return [self._message_view(record) for record in records]

    async def _attach_memory_receipts(self, records: list[MessageRecord], user_id: UUID) -> None:
        async with self._database.sessions() as session:
            jobs = list(
                await session.scalars(
                    select(JobRecord).where(
                        JobRecord.owner == str(user_id),
                        JobRecord.kind == "chat.memory",
                        JobRecord.task_run_id.in_([row.turn_id for row in records]),
                    )
                )
            )
        by_turn = {job.task_run_id: job for job in jobs}
        for row in records:
            job = by_turn.get(row.turn_id)
            if row.role != "assistant" or job is None:
                continue
            meta = dict(row.decision_meta or {})
            receipt = dict(meta.get("memory_receipt", {}))
            receipt["state"] = "completed" if job.status == "succeeded" else job.status
            receipt["error_code"] = job.error_code
            meta["memory_receipt"] = receipt
            row.decision_meta = meta

    async def send_message(
        self,
        conversation_id: UUID,
        *,
        user_id: UUID,
        text: str,
        privacy_level: PrivacyLevel,
        client_location: ClientLocation | None = None,
        client_request_id: str | None = None,
    ) -> ChatTurn:
        if client_request_id is not None:
            if client_location is not None:
                raise ValueError("idempotency_not_supported_for_ephemeral_location")
            replay = await self._replay_chat_request(
                user_id=user_id,
                conversation_id=conversation_id,
                text=text,
                privacy_level=privacy_level,
                client_request_id=client_request_id,
            )
            if replay is not None:
                return replay
        pending = await self.start_turn(
            conversation_id,
            user_id=user_id,
            text=text,
            privacy_level=privacy_level,
            client_location=client_location,
            client_request_id=client_request_id,
        )
        await self._transition(pending.turn_id, {"accepted"}, "thinking")
        try:
            await self._validate_context(pending)
            backend = self._budgeted_backend(pending)
            tool_executions: tuple[ToolExecution, ...] = ()
            consistency_request = pending.request
            transparency_reply = await self._transparency_report(pending)
            if transparency_reply is not None:
                # RPT：确定性汇报短路整条模型链路，杜绝编造
                result = CompletionResult(
                    text=transparency_reply,
                    provider="hub",
                    model="transparency-report",
                    endpoint="local",
                    route=pending.request.route,
                    finish_reason="stop",
                    latency_ms=0.0,
                )
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
            deterministic_call = _deterministic_home_read_call(pending)
            if deterministic_call is not None:
                execution = (await self._execute_tool_call(pending, [deterministic_call]))[0]
                tool_executions = (execution,)
                consistency_request = self._tool_result_request(
                    pending.request, deterministic_call, execution
                )
                result = await backend.complete(consistency_request)
            else:
                outcome = await self._run_agent_loop(pending, backend)
                result = outcome.result
                tool_executions = outcome.executions
                consistency_request = outcome.request
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
        finally:
            self._cancelled_generations.discard(pending.generation_id)

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
            # 多用户：无显式目标的主动消息优先业主的最近会话；无业主
            # 标识的库回落最早活跃用户（单用户行为），不随机跨用户投递
            row = (
                await session.execute(
                    query.order_by(
                        case((AppUserRecord.role == "owner", 0), else_=1),
                        ConversationRecord.last_active_at.desc(),
                    )
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
        client_request_id: str | None = None,
        parent_run_id: UUID | None = None,
        parent_budget_scope: dict[str, object] | None = None,
    ) -> PendingTurn:
        return await self._turn_lifecycle.start(
            conversation_id,
            user_id=user_id,
            text=text,
            privacy_level=privacy_level,
            max_context_messages=max_context_messages,
            client_location=client_location,
            llm_route=llm_route,
            client_request_id=client_request_id,
            parent_run_id=parent_run_id,
            parent_budget_scope=parent_budget_scope,
        )

    async def run_stream(
        self,
        pending: PendingTurn,
        on_delta: Callable[[str], Awaitable[None]],
        on_tool_event: Callable[[dict[str, object]], Awaitable[None]] | None = None,
    ) -> ChatTurn:
        await self._transition(pending.turn_id, {"accepted"}, "thinking")
        emitted = False
        stream_filter = ControlStreamFilter(extra_tags=pending.tool_names)
        buffer_for_consistency = self._memory_consistency_guard.requires_buffering(
            pending.memory_retrieval
        )

        async def guarded_delta(delta: str) -> None:
            nonlocal emitted
            self._check_cancelled(pending)
            if not emitted:
                await self._transition(pending.turn_id, {"thinking"}, "streaming")
                emitted = True
            elif pending.generation_id in self._cancelled_generations:
                # PERE-02：流式热路径取消检查走内存集合（cancel_turn 写入），
                # 不再每个 delta 查一次库；单进程部署形态下与本进程取消同源。
                raise TurnCancelled("generation_cancelled")
            await on_delta(delta)

        async def filtered_delta(delta: str) -> None:
            nonlocal emitted
            self._check_cancelled(pending)
            for visible in stream_filter.feed(delta):
                if buffer_for_consistency:
                    if not emitted:
                        await self._transition(pending.turn_id, {"thinking"}, "streaming")
                        emitted = True
                    elif pending.generation_id in self._cancelled_generations:
                        raise TurnCancelled("generation_cancelled")
                else:
                    await guarded_delta(visible)

        try:
            await self._validate_context(pending)
            backend = self._budgeted_backend(pending)
            consistency_request = pending.request
            tool_executions: tuple[ToolExecution, ...] = ()
            transparency_reply = await self._transparency_report(pending)
            deterministic_call = (
                None if transparency_reply is not None else _deterministic_home_read_call(pending)
            )
            if transparency_reply is not None:
                # RPT：确定性汇报短路模型链路；一致性缓冲模式下先过 Guard 再放行
                result = CompletionResult(
                    text=transparency_reply,
                    provider="hub",
                    model="transparency-report",
                    endpoint="local",
                    route=pending.request.route,
                    finish_reason="stop",
                    latency_ms=0.0,
                )
                if not buffer_for_consistency:
                    await filtered_delta(transparency_reply)
            elif deterministic_call is not None:
                if on_tool_event is not None:
                    await on_tool_event(
                        {
                            "type": "tool.started",
                            "tool": deterministic_call.function.name,
                            "label": _tool_label(deterministic_call.function.name),
                        }
                    )
                execution = (await self._execute_tool_call(pending, [deterministic_call]))[0]
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
            else:
                outcome = await self._run_agent_loop(
                    pending,
                    backend,
                    on_delta=filtered_delta,
                    on_tool_event=on_tool_event,
                )
                result = outcome.result
                tool_executions = outcome.executions
                consistency_request = outcome.request
            for visible in stream_filter.finish():
                if buffer_for_consistency:
                    if not emitted:
                        await self._transition(pending.turn_id, {"thinking"}, "streaming")
                        emitted = True
                    elif pending.generation_id in self._cancelled_generations:
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
                safe_text = parse_agent_reply(
                    consistency.result.text, pending.persona, tool_names=pending.tool_names
                ).text
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
        finally:
            self._cancelled_generations.discard(pending.generation_id)

    def _model_budget(
        self, pending: PendingTurn, *, phase: Literal["interactive", "maintenance"] = "interactive"
    ) -> RunModelBudget | None:
        enabled = (
            pending.parent_budget_enabled
            if pending.parent_run_id is not None
            else pending.config.run_budget.enabled
        )
        if not enabled:
            return None
        if pending.parent_budget_scope is not None:
            return ParentBudgetScope.read(pending.parent_budget_scope).restore(
                self._database, pending.config.run_budget, maintenance=phase == "maintenance"
            )
        return RunModelBudget(
            self._database,
            run_id=pending.parent_run_id or pending.turn_id,
            user_id=pending.user_id,
            config=pending.config.run_budget,
            phase=phase,
            allow_active_parent=pending.parent_run_id is not None and phase == "maintenance",
        )

    def _budgeted_backend(self, pending: PendingTurn) -> CompletionBackend:
        async def read_source() -> None:
            self._check_cancelled(pending)
            async with self._database.sessions() as session:
                if pending.parent_run_id is not None:
                    await require_chat_parent(
                        session,
                        pending.parent_run_id,
                        user_id=pending.user_id,
                        conversation_id=pending.conversation_id,
                        privacy_level=pending.request.privacy_level,
                        child_id=pending.turn_id,
                        quota_scope=pending.parent_budget_scope,
                    )
                run = await session.get(TaskRunRecord, pending.turn_id)
                owner = await session.get(AppUserRecord, pending.user_id)
                conversation = await session.get(ConversationRecord, pending.conversation_id)
                if (
                    run is None
                    or run.user_id != pending.user_id
                    or run.conversation_id != pending.conversation_id
                    or owner is None
                    or owner.status != "active"
                    or conversation is None
                    or conversation.user_id != pending.user_id
                ):
                    raise TurnCancelled("generation_source_unavailable")
                if run.status not in {"accepted", "running"} or run.contract.get(
                    "work_cancel_requested"
                ):
                    raise TurnCancelled("generation_cancelled")
                if run.deadline is not None and _aware(run.deadline) <= datetime.now(UTC):
                    raise BudgetDenied("run_deadline_exceeded")
                await assert_current_claim(session)
                await validate_references(
                    session,
                    pending.context_references,
                    owner_id=pending.user_id,
                    privacy_level=str(pending.request.privacy_level),
                )

        async def validate() -> None:
            if pending.parent_run_id is not None:
                await joined_read(read_source())
            else:
                await read_source()

        budget = self._model_budget(pending)
        return BudgetedBackend(
            self._router_builder(pending.config),
            budget,
            validate=validate,
            accounting=DisabledModelAccounting(
                self._database, run_id=pending.turn_id, user_id=pending.user_id
            )
            if budget is None
            else None,
        )

    async def _run_agent_loop(
        self,
        pending: PendingTurn,
        backend: CompletionBackend,
        *,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_event: Callable[[dict[str, object]], Awaitable[None]] | None = None,
    ) -> LoopOutcome[ToolExecution]:
        return await self._agent_loop.run(
            pending, backend, on_delta=on_delta, on_tool_event=on_tool_event
        )

    def _check_cancelled(self, pending: PendingTurn) -> None:
        self._agent_loop.check_cancelled(pending)

    async def _execute_tool_call(
        self,
        pending: PendingTurn,
        calls: list[ToolCall],
    ) -> list[ToolExecution]:
        return await self._agent_loop.execute_tool_call(pending, calls)

    async def _execute_single_call(
        self,
        pending: PendingTurn,
        call: ToolCall,
    ) -> ToolExecution:
        return await self._agent_loop.execute_single_call(pending, call)

    async def _transparency_report(self, pending: PendingTurn) -> str | None:
        return await self._agent_loop.transparency_report(pending)

    @staticmethod
    def _tool_result_request(
        request: CompletionRequest,
        selected_call: ToolCall,
        execution: ToolExecution,
        *,
        allow_followup_tools: bool = False,
    ) -> CompletionRequest:
        return AgentLoopHandlers.tool_result_request(
            request, selected_call, execution, allow_followup_tools=allow_followup_tools
        )

    def _recent_device_context(
        self,
        conversation_id: UUID,
        *,
        user_id: UUID,
        current_turn_seq: int,
        runtime_capabilities: tuple[RuntimeActionCapability, ...],
        now: datetime,
    ) -> str:
        return self._agent_loop.recent_device_context(
            conversation_id,
            user_id=user_id,
            current_turn_seq=current_turn_seq,
            runtime_capabilities=runtime_capabilities,
            now=now,
        )

    async def cancel_turn(
        self, generation_id: UUID, *, user_id: UUID, reason: str = "user_cancelled"
    ) -> bool:
        # Current turns cancel their run and required work in one transaction.
        # The nullable legacy path below remains for pre-TaskRun records.
        async with self._database.sessions() as session:
            source = (
                await session.execute(
                    select(InteractionTurnRecord.task_run_id, InteractionTurnRecord.state)
                    .join(
                        ConversationRecord,
                        ConversationRecord.id == InteractionTurnRecord.conversation_id,
                    )
                    .where(
                        InteractionTurnRecord.generation_id == generation_id,
                        ConversationRecord.user_id == user_id,
                    )
                )
            ).one_or_none()
        if source is None:
            raise LookupError("generation not found")
        if source[1] in {"cancelled", "failed", "completed"}:
            return False
        if source[0] is not None:
            cancelled, generations = await self.runs.cancel_work(
                source[0], user_id=user_id, reason=reason
            )
            self._cancelled_generations.update(generations)
            if cancelled and self._deleg_canceller is not None:
                with contextlib.suppress(Exception):
                    await self._deleg_canceller(source[0])
            return cancelled
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
            await transition_run(session, turn.id, "cancelled")
            cancelled_turn_id = turn.id
        # PERE-02：热路径取消检查的内存信号；回合收尾（提交/失败）时逐出。
        self._cancelled_generations.add(generation_id)
        if self._deleg_canceller is not None:
            # DELEG：取消回合即撤回本次委派的后台任务；失败只记日志
            with contextlib.suppress(Exception):
                await self._deleg_canceller(cancelled_turn_id)
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
        deletion_store = self._memory_store or MemoryStore(self._database)
        receipt = await deletion_store.hard_delete_by_source(
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
            if not await purge_conversation(session, conversation_id, user_id=user_id):
                raise LookupError("conversation not found")
        return receipt

    async def recover_incomplete_turns(self) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            run_ids = list(
                await session.scalars(
                    select(InteractionTurnRecord.task_run_id).where(
                        InteractionTurnRecord.state.in_({"accepted", "thinking", "streaming"}),
                        InteractionTurnRecord.task_run_id.is_not(None),
                    )
                )
            )
            for run_id in run_ids:
                if run_id is not None:
                    await transition_run(session, run_id, "cancelled")

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

        await recover_tool_reservations(self._database)
        await recover_expired_speech_deliveries(self._database)
        await recover_expired_model_runs(self._database)
        await recover_expired_deliveries(self._database)
        await recover_stale_reservations(self._database)

    def start_postcommit_worker(self) -> None:
        self._postcommit.start()
        if self._memory_store is not None and self._memory_decay_task is None:
            self._memory_decay_task = asyncio.create_task(self._decay_memory_loop())
        if self._memory_review_worker is not None:
            self._memory_review_worker.start()

    async def _decay_memory_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                if self._memory_store is not None:
                    await self._memory_store.decay_memories()
            except Exception:
                logger.exception("memory lifecycle maintenance failed")

    async def _commit_turn(
        self,
        pending: PendingTurn,
        result: CompletionResult,
        *,
        backend: CompletionBackend | None = None,
        consistency: MemoryConsistencyOutcome | None = None,
        tool_executions: tuple[ToolExecution, ...] = (),
    ) -> ChatTurn:
        return await self._commit_handlers.turn(
            pending,
            result,
            backend=backend,
            consistency=consistency,
            tool_executions=tool_executions,
        )

    async def _run_postcommit(self, kind: str, payload: dict[str, Any], owner: str) -> None:
        await self._postcommit_handlers.run(kind, payload, owner)

    async def _update_conversation_summary(
        self, conversation_id: UUID, *, strict: bool = False
    ) -> None:
        await self._postcommit_handlers.update_conversation_summary(conversation_id, strict=strict)

    async def _consolidate_memory(
        self,
        pending: PendingTurn,
        *,
        assistant_message: MessageView,
        backend: ExtractionBackend | None = None,
        strict: bool = False,
    ) -> None:
        await self._postcommit_handlers.consolidate_memory(
            pending, assistant_message=assistant_message, backend=backend, strict=strict
        )

    async def _extract_commitments(
        self,
        pending: PendingTurn,
        *,
        backend: Any | None = None,
        strict: bool = False,
    ) -> None:
        await self._postcommit_handlers.extract_commitments(pending, backend=backend, strict=strict)

    async def _assistant_profile_overrides(
        self, user_id: UUID, *, turn_id: UUID, privacy_level: PrivacyLevel
    ) -> tuple[dict[str, str], tuple[ContextReference, ...]]:
        """读取记忆库中用户明确告知的助手档案事实，按 fact_key 覆盖 Persona 基线。"""
        if self._memory_store is None:
            return {}, ()
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
            return {}, ()
        # 隐私闸门：档案覆盖会随系统提示进入每次模型调用，
        # L2 助手事实只能进入强制本地的 L2 上下文，绝不随 L0/L1 云端出站
        slots = [
            slot
            for slot in slots
            if slot.fact_key is not None and slot.privacy_level <= privacy_level.value
        ]
        overrides = {slot.fact_key: slot.content for slot in slots if slot.fact_key is not None}
        references = tuple(
            ContextReference(
                kind="memory",
                source_id=str(slot.id),
                owner_id=str(slot.user_id),
                privacy_level=slot.privacy_level,
                version=version_stamp(slot.updated_at),
                reason="assistant_profile",
            )
            for slot in slots
        )
        return overrides, references

    def _spawn_background(self, coroutine: Coroutine[None, None, None]) -> None:
        task = asyncio.create_task(coroutine, name="aria-memory-consolidation")
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def drain_background_work(self) -> None:
        """等待后台记忆任务完成；测试断言与优雅停机使用。"""
        if self._memory_decay_task is not None:
            self._memory_decay_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._memory_decay_task
            self._memory_decay_task = None
        await self._postcommit.stop()
        if self._memory_review_worker is not None:
            await self._memory_review_worker.stop()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks)
        # Explicit drains also process durable work when no lifespan worker ran.
        await self._postcommit.drain_ready()

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
            if target == "thinking":
                await transition_run(session, turn.id, "running")
            turn.state_version += 1

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
                await transition_run(session, turn.id, "failed")

    async def _replay_chat_request(
        self,
        *,
        user_id: UUID,
        conversation_id: UUID,
        text: str,
        privacy_level: PrivacyLevel,
        client_request_id: str,
    ) -> ChatTurn | None:
        async with self._database.sessions() as session:
            run = await session.scalar(
                select(TaskRunRecord).where(
                    TaskRunRecord.user_id == user_id,
                    TaskRunRecord.request_id == client_request_id,
                )
            )
            if run is None:
                return None
            user_message = await session.get(MessageRecord, UUID(run.contract["input_message_id"]))
            if (
                user_message is None
                or run.conversation_id != conversation_id
                or user_message.content != text
                or user_message.privacy_level != privacy_level.value
            ):
                raise ValueError("request_idempotency_conflict")
            if run.status != "succeeded":
                raise ValueError("request_already_accepted")
            assistant = await session.scalar(
                select(MessageRecord).where(
                    MessageRecord.turn_id == run.id,
                    MessageRecord.role == "assistant",
                    MessageRecord.conversation_id == conversation_id,
                )
            )
            if assistant is None:
                raise ValueError("request_result_unavailable")
            return ChatTurn(
                user_message=self._message_view(user_message),
                assistant_message=self._message_view(assistant),
            )

    async def cancel_run(self, run_id: UUID, *, user_id: UUID) -> bool:
        cancelled, generations = await self.runs.cancel_work(run_id, user_id=user_id)
        self._cancelled_generations.update(generations)
        return cancelled

    async def _validate_context(self, pending: PendingTurn) -> None:
        if pending.parent_run_id is not None:
            await joined_read(self._read_context_sources(pending))
        else:
            await self._read_context_sources(pending)

    async def _read_context_sources(self, pending: PendingTurn) -> None:
        async with self._database.sessions() as session:
            if pending.parent_run_id is not None:
                await require_chat_parent(
                    session,
                    pending.parent_run_id,
                    user_id=pending.user_id,
                    conversation_id=pending.conversation_id,
                    privacy_level=pending.request.privacy_level,
                    child_id=pending.turn_id,
                    quota_scope=pending.parent_budget_scope,
                )
            await validate_references(
                session,
                pending.context_references,
                owner_id=pending.user_id,
                privacy_level=str(pending.request.privacy_level),
            )

    async def _context_messages(
        self,
        conversation_id: UUID,
        *,
        limit: int = MAX_CONTEXT_MESSAGES,
        privacy_level: PrivacyLevel = PrivacyLevel.L2,
    ) -> list[MessageRecord]:
        async with self._database.sessions() as session:
            records = list(
                await session.scalars(
                    select(MessageRecord)
                    .where(
                        MessageRecord.conversation_id == conversation_id,
                        MessageRecord.privacy_level.in_(
                            [
                                level.value
                                for level in PrivacyLevel
                                if level.value <= privacy_level.value
                            ]
                        ),
                    )
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


_HOME_READ_FOLLOWUP_TERMS = (
    "然后呢",
    "查到了吗",
    "查到没",
    "有查到吗",
    "结果呢",
    "怎么样了",
)


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
