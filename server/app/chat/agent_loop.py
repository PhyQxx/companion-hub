"""Agent-loop orchestration and tool execution collaborator for ChatService."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from app.db import CognitiveDecisionRecord
from app.harness.budget import BudgetDenied, budget_scope, tool_budget_scope
from app.harness.guarded_call import guarded_inline_call
from app.harness.loop import CompletionFrame, LoopOutcome, run_agent_loop
from app.harness.run_trace import run_trace_scope
from app.llm import CompletionRequest, CompletionResult, LLMMessage, ToolCall
from app.llm.provider import EnvSecretProvider, absorb_text_tool_calls
from app.llm.text_tool_calls import MAX_TEXT_TOOL_CALLS
from app.runs.trace_sources import capture_disabled_trace
from app.schemas import PrivacyLevel
from app.tools import (
    ToolContext,
    ToolExecution,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    build_query_tool_runtime,
)

from .capabilities import RuntimeActionCapability
from .models import CompletionBackend, PendingTurn, RecentDeviceReference, TurnCancelled

if TYPE_CHECKING:
    from .service import ChatService

logger = logging.getLogger(__name__)

MULTI_CALL_SAFE_TOOLS = frozenset(
    {
        "get_location",
        "get_weather",
        "search_nearby",
        "plan_route",
        "home_get_state",
        "home_get_history",
        "search_devices",
        "mail_read",
        "mail_folders",
        "mail_attachments",
        "mail_sent",
        "contact_query",
        "commute_check",
        "focus_status",
        "reminder_list",
        "inspect_webpage",
        "fetch_webpage",
        "hermes_status",
        "propose_skill",
        "pnkx_read_life",
    }
)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# RPT（docs/09 §7）：透明度问询的确定性意图词；命中后不经模型，
# 直接从 CognitiveDecision 记录渲染汇报，杜绝编造。
_TRANSPARENCY_TERMS = (
    "主动做了什么",
    "主动说了什么",
    "主动提过什么",
    "为什么提醒我",
    "为什么打扰我",
    "今天主动开口",
)

_TRANSPARENCY_LABELS = {
    "inform": "告知",
    "ask": "询问",
    "suggest": "建议",
    "escalate": "紧急升级",
}


def render_transparency_report(rows: list[CognitiveDecisionRecord], *, zone: ZoneInfo) -> str:
    """把近 24 小时的认知决策渲染为可问责的确定性汇报文本。"""
    visible = [row for row in rows if row.decision in _TRANSPARENCY_LABELS]
    silent_count = len(rows) - len(visible)
    if not visible:
        return (
            "最近 24 小时我没有主动开过口"
            + (f"，另有 {silent_count} 件事我注意到但选择了保持安静。" if silent_count else "。")
            + "每次判断的完整依据都在时间线里，可随时追查。"
        )
    lines = [f"最近 24 小时我主动开口 {len(visible)} 次："]
    for row in visible[:8]:
        clock = _aware(row.created_at).astimezone(zone).strftime("%H:%M")
        reasons = f"（{'、'.join(row.reason_codes[:2])}）" if row.reason_codes else ""
        lines.append(f"· {clock} {_TRANSPARENCY_LABELS[row.decision]}：{row.trigger_kind}{reasons}")
    if len(visible) > 8:
        lines.append(f"· ……以及另外 {len(visible) - 8} 次")
    if silent_count:
        lines.append(f"另有 {silent_count} 件事我注意到但选择了保持安静。")
    lines.append("每次决定的完整依据都在时间线里，可随时追查。")
    return "\n".join(lines)


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
        if result.reason_code == "integration_token_rejected":
            return (
                "旧 PNKX 生活连接的集成令牌被拒绝；这次没有使用技能中心连接。"
                "若已授权相关技能，请确认当前为 L1 会话；否则检查旧连接的令牌。"
            )
        return f"PNKX 操作没有完成（{result.reason_code or 'unknown_error'}）。"
    resource = str(result.data.get("resource") or "data")
    label = _PNKX_RESOURCE_LABELS.get(resource, "数据")
    if result.tool_name == "pnkx_create_life":
        return f"已在 PNKX 创建{label}。"
    if result.tool_name == "pnkx_update_life":
        return f"已更新 PNKX {label}。"
    if result.tool_name == "pnkx_delete_life":
        return f"已删除 PNKX {label}。"

    items = result.data.get("items")
    if isinstance(items, list):
        total_value = result.data.get("total")
        total = total_value if isinstance(total_value, int) else len(items)
        if not items:
            return f"PNKX 中没有找到{label}。"
        lines = [f"PNKX 中共有 {total} 条{label}，前 {min(len(items), 5)} 条是："]
        lines.extend(
            f"{index}. {_pnkx_item_summary(item)}" for index, item in enumerate(items[:5], start=1)
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


def _tool_label(tool_name: str) -> str:
    return {
        "get_location": "正在确认当前位置…",
        "get_weather": "正在查询天气…",
        "search_nearby": "正在查找附近地点…",
        "plan_route": "正在规划路线…",
        "capture_screen": "正在读取电脑屏幕…",
        "inspect_webpage": "正在读取当前网页…",
        "fetch_webpage": "正在读取网页…",
        "home_get_state": "正在读取设备状态…",
        "home_get_history": "正在读取设备历史…",
        "home_control": "正在执行设备控制…",
        "pnkx_read_life": "正在读取 pnkx 生活数据…",
        "pnkx_create_life": "正在写入 pnkx 生活数据…",
        "pnkx_update_life": "正在更新 pnkx 生活数据…",
        "pnkx_delete_life": "正在删除 pnkx 生活数据…",
        "reminder_create": "正在创建提醒…",
        "reminder_list": "正在查看提醒列表…",
        "reminder_cancel": "正在关闭提醒…",
        "calendar_create": "正在创建日程…",
        "contact_save": "正在保存联系人…",
        "commute_check": "正在规划出行…",
        "focus_start": "正在开始专注…",
        "focus_status": "正在查看专注状态…",
        "contact_query": "正在查找联系人…",
        "mail_read": "正在读取邮箱…",
        "mail_send": "正在发送邮件…",
        "mail_sent": "正在查发送记录…",
        "delegate_task": "正在转入后台处理…",
        "propose_action": "正在草拟待确认的操作…",
    }.get(tool_name, "正在使用外部工具…")


class AgentLoopHandlers:
    """代理循环与工具执行的协作者；经 service 薄包装保留既有调用语义。"""

    def __init__(self, service: ChatService) -> None:
        self._service = service

    async def run(
        self,
        pending: PendingTurn,
        backend: CompletionBackend,
        *,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_event: Callable[[dict[str, object]], Awaitable[None]] | None = None,
    ) -> LoopOutcome[ToolExecution]:
        async def complete(request: CompletionRequest, final: bool) -> CompletionFrame:
            try:
                await self._service._validate_context(pending)
                self.check_cancelled(pending)
                if on_delta is None:
                    return CompletionFrame(await backend.complete(request))
                if final:
                    return CompletionFrame(await backend.stream(request, on_delta))
                chunks: list[str] = []

                async def buffer(delta: str) -> None:
                    if delta:
                        chunks.append(delta)

                return CompletionFrame(await backend.stream(request, buffer), tuple(chunks))

            except BudgetDenied as error:
                if error.reason_code != "run_budget_exhausted" or not request.tools:
                    raise
                terminal = request.model_copy(
                    update={
                        "tools": [],
                        "tool_choice": "none",
                        "messages": [
                            *request.messages,
                            LLMMessage(
                                role="system",
                                content=(
                                    "本轮工具调用额度已用完。只根据已有证据给出终答，明确说明未完成的查询或行动；"
                                    "不得编造新结果，也不得宣称未执行的操作已完成。"
                                ),
                            ),
                        ],
                    }
                )
                frame = await complete(terminal, True)
                return CompletionFrame(
                    frame.result.model_copy(
                        update={
                            "context_budget": {
                                **frame.result.context_budget,
                                "budget_final_answer": 1,
                            }
                        }
                    ),
                    frame.buffered_chunks,
                    effective_request=terminal,
                )

        async def execute(calls: list[ToolCall]) -> list[ToolExecution]:
            if on_tool_event is not None:
                for call in calls:
                    await on_tool_event(
                        {
                            "type": "tool.started",
                            "tool": call.function.name,
                            "label": _tool_label(call.function.name),
                        }
                    )
            executions = await self.execute_tool_call(pending, calls)
            if on_tool_event is not None:
                for execution in executions:
                    await on_tool_event(
                        {
                            "type": "tool.finished",
                            "tool": execution.result.tool_name,
                            "ok": execution.result.ok,
                            "latency_ms": round(execution.result.latency_ms, 1),
                            "reason_code": execution.result.reason_code,
                        }
                    )
            return executions

        def followup(
            request: CompletionRequest,
            result: CompletionResult,
            executions: list[ToolExecution],
            allow_tools: bool,
        ) -> CompletionRequest:
            return self.tool_followup_request(
                request, result, executions, allow_followup_tools=allow_tools
            )

        def direct_reply(executions: list[ToolExecution]) -> str | None:
            if len(executions) != 1:
                return None
            receipt = executions[0].result
            # Keep the existing streaming PNKX renderer; ordinary text uses its
            # model followup. Receipt policy is application-owned, not harness.
            if on_delta is not None and receipt.tool_name.startswith("pnkx_"):
                return _render_pnkx_tool_reply(receipt)
            return _render_mail_send_receipt(receipt)

        def check_cancelled() -> None:
            self.check_cancelled(pending)

        async def deliver(frame: CompletionFrame) -> None:
            if on_delta is not None:
                for chunk in frame.buffered_chunks:
                    check_cancelled()
                    await on_delta(chunk)

        return await run_agent_loop(
            pending.request,
            max_tool_rounds=pending.config.tools.max_tool_rounds,
            complete=complete,
            normalize=absorb_text_tool_calls,
            execute=execute,
            followup=followup,
            direct_reply=direct_reply,
            check_cancelled=check_cancelled,
            deliver=deliver,
        )

    def check_cancelled(self, pending: PendingTurn) -> None:
        if (
            pending.config.run_budget.enabled
            and (datetime.now(UTC) - _aware(pending.user_message.created_at)).total_seconds()
            >= pending.config.run_budget.interactive_deadline_seconds
        ):
            raise BudgetDenied("run_deadline_exceeded")
        if pending.generation_id in self._service._cancelled_generations:
            raise TurnCancelled("generation_cancelled")

    async def execute_tool_call(
        self,
        pending: PendingTurn,
        calls: list[ToolCall],
    ) -> list[ToolExecution]:
        await self._service._validate_context(pending)
        budget = self._service._model_budget(pending)
        trace = (
            await capture_disabled_trace(
                self._service._database, pending.turn_id, pending.user_id, require_disabled=True
            )
            if budget is None and pending.request.privacy_level != PrivacyLevel.L3
            else None
        )
        with budget_scope(budget), tool_budget_scope(None), run_trace_scope(trace):
            if pending.parent_run_id is not None:
                return await guarded_inline_call(
                    lambda: self.execute_tool_calls_body(pending, calls),
                    lambda: self._service._validate_context(pending),
                )
            return await self.execute_tool_calls_body(pending, calls)

    async def execute_tool_calls_body(
        self,
        pending: PendingTurn,
        calls: list[ToolCall],
    ) -> list[ToolExecution]:
        """BTL-02：单轮执行一组调用。

        单调用维持既有语义；多调用仅当全部命中只读安全集时逐个执行，
        否则不执行任何调用并回 multiple_tool_calls_not_allowed 让模型
        自我修正——绝不批量执行含写动作的组合。
        """
        if len(calls) == 1:
            return [await self.execute_single_call(pending, calls[0])]
        if not calls:
            return []
        unsafe = next((call for call in calls if not self.multi_call_safe(call, pending)), None)
        if unsafe is not None:
            return [
                ToolExecution(
                    call_id=unsafe.id,
                    result=ToolResult(
                        ok=False,
                        tool_name=unsafe.function.name,
                        reason_code="multiple_tool_calls_not_allowed",
                        latency_ms=0,
                    ),
                )
            ]
        return [
            await self.execute_single_call(pending, call) for call in calls[:MAX_TEXT_TOOL_CALLS]
        ]

    def multi_call_safe(self, call: ToolCall, pending: PendingTurn) -> bool:
        name = call.function.name
        if name in MULTI_CALL_SAFE_TOOLS:
            return True
        if name.startswith("skill."):
            # 技能只读工具按契约只允许 GET 挂载进对话
            return True
        # 本轮挂载的 MCP 工具均为只读（写动作只经计划确认链）
        return any(handler.name == name for handler in pending.mcp_handlers)

    async def execute_single_call(self, pending: PendingTurn, call: ToolCall) -> ToolExecution:
        execution = await self._execute_single_call_body(pending, call)
        if (
            not execution.result.ok
            or not self.multi_call_safe(call, pending)
            or pending.memory_retrieval is None
        ):
            return execution
        # Evidence IDs are produced by the server from successful read operations,
        # never accepted from model/handler claims. Do not persist raw arguments.
        arguments = call.function.arguments

        def strings(value: object) -> list[str]:
            if isinstance(value, str):
                return [value]
            if isinstance(value, dict):
                return [text for item in value.values() for text in strings(item)]
            if isinstance(value, list):
                return [text for item in value for text in strings(item)]
            return []

        values = strings(arguments)
        included = {
            ref.source_id
            for ref in pending.context_references
            if ref.kind == "memory" and ref.included
        }
        identifiers = []
        for hit in pending.memory_retrieval.hits:
            evidence = hit.memory.content.removeprefix("用户").strip("。 .，,")
            if (
                str(hit.memory.id) in included
                and len(evidence) >= 6
                and any(evidence in value for value in values)
            ):
                identifiers.append(hit.memory.id)
        return execution.model_copy(update={"memory_ids_used": tuple(identifiers)})

    async def _execute_single_call_body(
        self,
        pending: PendingTurn,
        call: ToolCall,
    ) -> ToolExecution:
        self.check_cancelled(pending)
        if call.function.name not in pending.tool_names:
            return ToolExecution(
                call_id=call.id,
                result=ToolResult(
                    ok=False,
                    tool_name=call.function.name,
                    reason_code="tool_not_mounted",
                    latency_ms=0,
                ),
            )
        context = ToolContext(
            privacy_level=pending.request.privacy_level,
            user_id=pending.user_id,
            turn_id=pending.turn_id,
            user_text=pending.user_message.content,
            current_time=pending.user_message.created_at,
            timezone_name=pending.user_timezone,
            default_city=pending.config.tools.query.default_city,
            ephemeral_location=pending.client_location,
        )
        service = self._service
        if service._device_tools.get(call.function.name) is not None:
            try:
                execution = await ToolExecutor(service._device_tools).execute(
                    call,
                    context,
                )
                self.remember_device(pending, execution.result)
                return execution
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
        mcp_handler = next(
            (item for item in pending.mcp_handlers if item.name == call.function.name), None
        )
        if mcp_handler is not None:
            # 本轮选中的只读 MCP 工具：独立注册表执行，L1 上限由 Egress 兜底
            try:
                execution = await ToolExecutor(ToolRegistry([mcp_handler])).execute(
                    call,
                    context,
                )
                return execution
            except Exception:
                logger.warning(
                    "mcp tool runtime failed turn_id=%s tool=%s",
                    pending.turn_id,
                    call.function.name,
                    exc_info=True,
                )
                return ToolExecution(
                    call_id=call.id,
                    result=ToolResult(
                        ok=False,
                        tool_name=call.function.name,
                        reason_code="mcp_tool_unavailable",
                        latency_ms=0,
                    ),
                )
        skill_handler = next(
            (item for item in pending.skill_handlers if item.name == call.function.name), None
        )
        if skill_handler is not None:
            return await ToolExecutor(ToolRegistry([skill_handler])).execute(call, context)
        if (
            service._skill_drafts is not None
            and call.function.name == service._skill_drafts.name
            and service._skill_drafts.name in pending.tool_names
        ):
            return await ToolExecutor(ToolRegistry([service._skill_drafts])).execute(call, context)
        if (
            service._web_fetch is not None
            and call.function.name == service._web_fetch.name
            and service._web_fetch.name in pending.tool_names
        ):
            return await ToolExecutor(ToolRegistry([service._web_fetch])).execute(call, context)
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

    async def transparency_report(self, pending: PendingTurn) -> str | None:
        """RPT（docs/09 §7）：透明度问询的确定性汇报，不经模型。"""
        text = pending.user_message.content
        if not any(term in text for term in _TRANSPARENCY_TERMS):
            return None
        since = _aware(pending.user_message.created_at) - timedelta(hours=24)
        try:
            async with self._service._database.sessions() as session:
                rows = list(
                    await session.scalars(
                        select(CognitiveDecisionRecord)
                        .where(
                            CognitiveDecisionRecord.user_id == pending.user_id,
                            CognitiveDecisionRecord.created_at >= since,
                        )
                        .order_by(CognitiveDecisionRecord.created_at.desc())
                        .limit(100)
                    )
                )
        except Exception:
            logger.warning(
                "transparency report lookup failed for turn %s", pending.turn_id, exc_info=True
            )
            return None
        try:
            zone = ZoneInfo(pending.user_timezone)
        except ZoneInfoNotFoundError:
            zone = ZoneInfo("Asia/Shanghai")
        return render_transparency_report(rows, zone=zone)

    def remember_device(self, pending: PendingTurn, result: ToolResult) -> None:
        if not result.ok or result.tool_name not in {
            "search_devices",
            "home_get_state",
            "home_get_history",
            "home_control",
        }:
            return
        candidates: list[dict[str, Any]] = []
        if result.tool_name == "search_devices":
            raw = result.data.get("devices")
            if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], dict):
                match_kind = raw[0].get("match_kind")
                if match_kind in {"entity_exact", "name_exact", "alias_exact"}:
                    candidates = [raw[0]]
        elif result.tool_name == "home_get_state":
            raw = result.data.get("entities")
            if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], dict):
                candidates = [raw[0]]
        else:
            candidates = [result.data]
        if len(candidates) != 1:
            return
        entity_id = candidates[0].get("entity_id")
        name = candidates[0].get("name")
        if not isinstance(entity_id, str) or not isinstance(name, str):
            return
        reference = RecentDeviceReference(
            entity_id=entity_id,
            name=name,
            user_id=pending.user_id,
            source_turn_seq=pending.turn_seq,
            updated_at=datetime.now(UTC),
        )
        existing = [
            item
            for item in self._service._recent_devices.get(pending.conversation_id, [])
            if item.entity_id != entity_id
        ]
        self._service._recent_devices[pending.conversation_id] = [reference, *existing][:3]

    def recent_device_context(
        self,
        conversation_id: UUID,
        *,
        user_id: UUID,
        current_turn_seq: int,
        runtime_capabilities: tuple[RuntimeActionCapability, ...],
        now: datetime,
    ) -> str:
        authorized = {
            item.capability_id.split(":", 2)[1]
            for item in runtime_capabilities
            if item.capability_id.startswith("home_assistant:")
        }
        valid = [
            item
            for item in self._service._recent_devices.get(conversation_id, [])
            if item.user_id == user_id
            and item.entity_id in authorized
            and current_turn_seq - item.source_turn_seq <= 3
            and now - item.updated_at <= timedelta(minutes=10)
        ]
        self._service._recent_devices[conversation_id] = valid
        if len(valid) != 1:
            return ""
        item = valid[0]
        return (
            f"【近期设备指代】上一轮明确设备为 {item.name}（{item.entity_id}）。"
            "仅可用于理解「它」指哪台设备；不能视为动作确认，调用工具时必须重新校验权限。"
        )

    @staticmethod
    def tool_followup_request(
        request: CompletionRequest,
        first_result: CompletionResult,
        executions: list[ToolExecution],
        *,
        allow_followup_tools: bool = False,
    ) -> CompletionRequest:
        """按执行结果构建跟进请求（BTL-02：单轮多个只读调用全部回填）。

        每个执行结果对应一条 tool 消息；assistant 消息携带对应的全部
        调用（协议要求每个 tool_call 都有应答）。单执行路径保持既有
        语义（只回填该调用）。
        """
        by_id = {execution.call_id: execution for execution in executions}
        selected: list[ToolCall] = []
        for call in first_result.tool_calls:
            if call.id in by_id:
                selected.append(call)
        if not selected:
            selected = [first_result.tool_calls[0]]
        messages: list[LLMMessage] = [
            *request.messages,
            LLMMessage(role="assistant", content="", tool_calls=selected),
        ]
        for call in selected:
            execution = by_id.get(call.id) or executions[0]
            messages.append(
                LLMMessage(
                    role="tool",
                    tool_call_id=call.id,
                    name=call.function.name,
                    content=json.dumps(
                        execution.result.model_dump(mode="json"),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            )
        return CompletionRequest.model_validate(
            {
                **request.model_dump(mode="python"),
                "messages": messages,
                # 最后一轮收束为无工具纯文本；中间轮保留目录允许链式调用。
                "tools": request.tools if allow_followup_tools else [],
                "tool_choice": "auto" if allow_followup_tools else "none",
            }
        )

    @staticmethod
    def tool_result_request(
        request: CompletionRequest,
        selected_call: ToolCall,
        execution: ToolExecution,
        *,
        allow_followup_tools: bool = False,
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
                # 最后一轮收束为无工具纯文本；中间轮保留目录允许链式调用。
                "tools": request.tools if allow_followup_tools else [],
                "tool_choice": "auto" if allow_followup_tools else "none",
            }
        )
