"""Turn commit collaborator for ChatService: resolve, audit, persist, publish."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.db import ConversationRecord, InteractionTurnRecord, MessageRecord
from app.ids import uuid7
from app.llm import CompletionResult
from app.runs.chat_parent import require_chat_parent, validate_parent
from app.runs.store import transition_run
from app.schemas import AgentReply
from app.timeline import RecallMode
from app.tools import ToolExecution, ToolResult

from .context_sources import validate_references
from .memory_consistency import MemoryConsistencyOutcome
from .models import ChatTurn, CompletionBackend, PendingTurn, TurnCancelled
from .postcommit import RESOURCE as POSTCOMMIT_RESOURCE
from .postcommit_handlers import SUMMARY_TRIGGER_MESSAGES
from .reply import parse_agent_reply

if TYPE_CHECKING:
    from app.cognition import CognitiveDecision

    from .service import ChatService

logger = logging.getLogger(__name__)


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


class CommitHandlers:
    """回合提交协作者；经 service 薄包装保留既有调用语义。"""

    def __init__(self, service: ChatService) -> None:
        self._service = service

    async def turn(
        self,
        pending: PendingTurn,
        result: CompletionResult,
        *,
        backend: CompletionBackend | None = None,
        consistency: MemoryConsistencyOutcome | None = None,
        tool_executions: tuple[ToolExecution, ...] = (),
    ) -> ChatTurn:
        consistency, result, reply = await self.resolve(
            pending,
            result,
            backend=backend,
            consistency=consistency,
            tool_executions=tool_executions,
        )
        decision_meta = await self.decision_meta(
            pending, result, consistency, reply, tool_executions=tool_executions
        )
        assistant_record = await self.persist(pending, reply, decision_meta)
        return await self.publish(pending, assistant_record)

    async def resolve(
        self,
        pending: PendingTurn,
        result: CompletionResult,
        *,
        backend: CompletionBackend | None,
        consistency: MemoryConsistencyOutcome | None,
        tool_executions: tuple[ToolExecution, ...],
    ) -> tuple[MemoryConsistencyOutcome, CompletionResult, AgentReply]:
        if consistency is None:
            consistency = await self._service._memory_consistency_guard.enforce(
                result=result,
                request=pending.request,
                persona=pending.persona,
                retrieval=pending.memory_retrieval,
                backend=backend,
            )
            result = consistency.result
        reply = parse_agent_reply(result.text, pending.persona, tool_names=pending.tool_names)
        if reply.parse_status == "fallback":
            # 兜底回复意味着模型没给出可用文本：把当轮模型、结束原因与工具
            # 失败摘要打进控制台，否则「没有生成有效回复」无从排查。
            logger.warning(
                "assistant reply fell back: model=%s finish=%s tool_failures=%s raw_head=%r",
                result.model,
                result.finish_reason,
                [
                    (e.result.tool_name, e.result.reason_code)
                    for e in tool_executions
                    if not e.result.ok
                ]
                or "none",
                result.text[:120],
            )
        return consistency, result, reply

    async def decision_meta(
        self,
        pending: PendingTurn,
        result: CompletionResult,
        consistency: MemoryConsistencyOutcome,
        reply: AgentReply,
        *,
        tool_executions: tuple[ToolExecution, ...],
    ) -> dict[str, object]:
        decision_meta: dict[str, object] = {
            "schema_version": 1,
            "task_run_id": str(pending.turn_id),
            "context_sources": list(pending.context_manifest),
            "context_budget": result.context_budget,
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
        service = self._service
        if service._avatar_store is not None and pending.persona_version is not None:
            try:
                default_avatar = await service._avatar_store.get_default_for_persona(
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
                    **(
                        {
                            "skill_version": next(
                                (
                                    handler.skill_version
                                    for handler in pending.skill_handlers
                                    if handler.name == execution.result.tool_name
                                ),
                                None,
                            )
                        }
                        if execution.result.tool_name.startswith("skill.")
                        else {}
                    ),
                    "result_count": _tool_result_count(execution.result),
                    "cache_hit": execution.result.cache_hit,
                    # docs/05 地图/天气章节 §6.2: 只记解析来源枚举, 不记坐标或原始地址。
                    "location_source": execution.result.location_source,
                }
                for execution in tool_executions
            ]
            if len(tool_executions) > 1:
                # 有界工具循环的多轮轨迹（docs/09 §1）；单轮时 tool_calls 已足够。
                decision_meta["tool_rounds"] = [
                    execution.result.tool_name for execution in tool_executions
                ]
            presentation = _tool_presentation(tool_executions[-1].result)
            if presentation is not None:
                # 只落前端展示所需的公开字段，不含用户起点坐标或 provider 原始响应。
                decision_meta["tool_result"] = presentation
        grounded_memory_hits = (
            service._memory_retriever.grounded_hits(pending.memory_retrieval)
            if service._memory_retriever is not None and pending.memory_retrieval is not None
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
        elif pending.browser_activity_recall is not None:
            browser_recall = pending.browser_activity_recall
            decision_meta["recall"] = {
                "mode": "browser_activity",
                "timeline_ids": [item.id for item in browser_recall.events],
                "source_ids": [item.source_id for item in browser_recall.events],
                "time_range": {
                    "start_at": browser_recall.temporal_range.start_at.isoformat(),
                    "end_at": browser_recall.temporal_range.end_at.isoformat(),
                },
                "candidate_count": browser_recall.candidate_count,
                "segment_count": len(browser_recall.segments),
                "truncated": browser_recall.truncated,
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
        return decision_meta

    async def persist(
        self,
        pending: PendingTurn,
        reply: AgentReply,
        decision_meta: dict[str, object],
    ) -> MessageRecord:
        assistant_time = datetime.now(UTC)
        service = self._service
        async with service._database.sessions.begin() as session:
            parent = None
            if pending.parent_run_id is not None:
                parent = await require_chat_parent(
                    session,
                    pending.parent_run_id,
                    user_id=pending.user_id,
                    conversation_id=pending.conversation_id,
                    privacy_level=pending.request.privacy_level,
                    child_id=pending.turn_id,
                    lock=True,
                    quota_scope=pending.parent_budget_scope,
                )
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
            if (
                conversation is None
                or conversation.user_id != pending.user_id
                or conversation.status != "active"
                or turn is None
            ):
                raise LookupError("conversation not found")
            if turn.state == "cancelled":
                raise TurnCancelled("generation_cancelled")
            if turn.state not in {"thinking", "streaming"}:
                raise RuntimeError("turn is not committable")
            await validate_references(
                session,
                pending.context_references,
                owner_id=pending.user_id,
                privacy_level=str(pending.request.privacy_level),
                lock=True,
            )
            conversation.last_seq += 1
            conversation.last_active_at = assistant_time
            # CTX：事务内同步判断是否达到摘要阈值，达标才派生后台任务
            summary_due = (
                conversation.last_seq - (conversation.summary_until_seq or 0)
                >= SUMMARY_TRIGGER_MESSAGES
            )
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
            await transition_run(session, pending.turn_id, "succeeded")
            operations = ["timeline"] if service._timeline_store is not None else []
            if service._memory_ingester is not None:
                operations.append("memory")
            if service._goal_tracker is not None:
                operations.append("commitments")
            if service._skill_drafts is not None:
                operations.append("skill_draft")
            if service._skill_learner is not None:
                operations.append("skill_revision")
            if summary_due:
                operations.append("summary")
            for operation in operations:
                await service._postcommit.engine.submit_in_session(
                    session,
                    f"chat.{operation}",
                    {
                        "assistant_message_id": str(assistant_record.id),
                        "user_timezone": pending.user_timezone,
                    },
                    owner=str(pending.user_id),
                    resource_class=POSTCOMMIT_RESOURCE,
                    idempotency_key=f"chat:{pending.turn_id}:{operation}",
                    task_run_id=pending.turn_id,
                )
            if parent is not None:
                await session.flush()
                validate_parent(
                    parent,
                    user_id=pending.user_id,
                    conversation_id=pending.conversation_id,
                    privacy_level=pending.request.privacy_level,
                    quota_scope=pending.parent_budget_scope,
                )
        return assistant_record

    async def publish(self, pending: PendingTurn, assistant_record: MessageRecord) -> ChatTurn:
        turn_result = ChatTurn(
            user_message=pending.user_message,
            assistant_message=self._service._message_view(assistant_record),
        )
        # Timeline indexing is local and historically visible when send returns.
        # Keep that behavior; its durable job safely repairs an interrupted index.
        if self._service._timeline_store is not None:
            await self._service._postcommit_handlers.index_timeline(
                pending, assistant_message=turn_result.assistant_message
            )
        # The lifespan-owned worker polls the committed queue. Standalone
        # service callers explicitly start it or drain pending work; never
        # create an unowned DB task that can outlive its event loop/engine.
        return turn_result
