"""BTL-03（docs/09 §1）：对话内提议注册表动作，经计划确认闭环执行。

模型在聊天里无法直接执行桌面/浏览器/技能写等 A 级动作（它们刻意不
作为聊天工具挂载）；`propose_action` 让模型把这类意图转成待确认的
行动计划——A2 动作停在 awaiting_confirmation，A1 动作形成 ready
计划——由用户在计划卡片上确认/执行。计划全部完成后经
`PlanCompletionReporter` 主动汇报结果，形成"提议→确认→执行→汇报"
的断点续跑闭环。A3 禁止动作与未注册动作一律拒绝。
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import logging
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Annotated, Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.llm import ToolDefinition
from app.schemas.common import PrivacyLevel
from app.tools.contracts import ToolContext, ToolResult

from .action_plan import ActionPlanService, ActionPlanView
from .action_registry import ActionRegistry, ActionRisk

logger = logging.getLogger("app.cognition.propose")

ProactiveDeliver = Callable[..., Awaitable[Any]]

MAX_STEPS = 5


class ProposeActionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: Annotated[str, Field(min_length=1, max_length=160)]
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    # 向用户解释为什么提议这个动作（会出现在确认卡上）
    note: Annotated[str, Field(min_length=1, max_length=300)]


class ProposeActionTool:
    """对话内动作提议：只创建待确认计划，绝不直接执行。"""

    name = "propose_action"
    description = (
        "提议执行一个有实际影响的操作（打开应用/网页、浏览器填表提交、"
        "写剪贴板、技能写数据等）。动作不会立即执行——会生成待确认的行动"
        "计划，用户在计划卡片上确认后才执行，完成后我会主动汇报结果。"
        "当用户要求做这类操作时调用；只读查询请用常规查询工具。仅 L1。"
    )
    arguments_model: type[BaseModel] = ProposeActionArgs
    max_privacy_level = PrivacyLevel.L1
    runs_local = True

    def __init__(
        self,
        plan_service_getter: Callable[[], ActionPlanService | None],
        registry: ActionRegistry,
    ) -> None:
        # main 中计划服务晚于工具装配完成 runner 注入，用惰性获取解耦。
        self._plan_service_getter = plan_service_getter
        self._registry = registry

    @property
    def available(self) -> bool:
        return True

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=ProposeActionArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(ProposeActionArgs, arguments)
        if context.privacy_level != PrivacyLevel.L1:
            return self._failure("private_session_unsupported", started)
        if context.user_id is None or context.turn_id is None:
            return self._failure("turn_context_missing", started)
        plan_service = self._plan_service_getter()
        if plan_service is None:
            return self._failure("plan_service_unavailable", started)
        registered = self._registry.get(args.action_id)
        if registered is None:
            # 近邻提示让模型在下一工具轮自纠错（typo 类失败在会话内闭环）
            suggestions = difflib.get_close_matches(
                args.action_id,
                [definition.action_id for definition in self._registry.definitions()],
                n=2,
                cutoff=0.6,
            )
            logger.warning(
                "propose_action rejected: action_id 不在注册表 action_id=%r"
                "（模型笔误或注册表未同步；近邻=%s）",
                args.action_id,
                suggestions or "无",
            )
            return self._failure(
                "action_unknown", started, {"suggest": suggestions} if suggestions else None
            )
        # definition.risk 经 pydantic 存储后是字符串值，这里归一回枚举
        try:
            risk = ActionRisk(registered.definition.risk)
        except ValueError:
            return self._failure("action_unknown", started)
        if risk is ActionRisk.A3_PROHIBITED:
            return self._failure("action_prohibited", started)
        if risk not in {ActionRisk.A1_LOW, ActionRisk.A2_CONFIRM}:
            # A0 只读动作不需要确认链，用常规查询工具表达
            return self._failure("action_is_readonly", started)
        try:
            plan = await plan_service.create_plan(
                user_id=context.user_id,
                invocations=[_invocation(args.action_id, dict(args.arguments))],
                title=f"提议：{registered.definition.label}"[:240],
                idempotency_key=f"propose-{context.turn_id}-{args.action_id}",
                ttl_seconds=600,
            )
        except PermissionError:
            return self._failure("action_prohibited", started)
        except ValueError as error:
            # 参数未过编译校验（必填缺失/类型不符/多余字段）。
            # 细节透传给模型：收尾补全看得懂缺什么，才能向用户说明或改参重试。
            detail = str(error).strip()[:300]
            logger.warning(
                "propose_action rejected: 参数未过校验 action_id=%r detail=%s",
                args.action_id,
                detail,
            )
            return self._failure(
                "arguments_invalid", started, {"error_detail": detail} if detail else None
            )
        return ToolResult(
            ok=True,
            tool_name=self.name,
            provider="action-plan",
            latency_ms=(perf_counter() - started) * 1_000,
            data=_plan_card(plan, args.note),
        )

    def _failure(
        self, reason: str, started: float, data: dict[str, Any] | None = None
    ) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=self.name,
            reason_code=reason,
            data=data or {},
            latency_ms=(perf_counter() - started) * 1_000,
        )


def _invocation(action_id: str, arguments: dict[str, JsonValue]):  # type: ignore[no-untyped-def]
    from .action_plan import ActionInvocation

    return ActionInvocation(action_id=action_id, arguments=arguments)


def _plan_card(plan: ActionPlanView, note: str) -> dict[str, Any]:
    step = plan.steps[0]
    return {
        "plan_id": str(plan.id),
        "status": plan.status,
        "action_id": step.action_id,
        "risk": step.risk,
        "note": note,
        "expires_at": plan.expires_at.isoformat(),
        "ack": (
            "已生成待确认的操作计划，请在计划卡片上确认。"
            if plan.status == "awaiting_confirmation"
            else "已生成操作计划，确认后即可执行。"
        ),
    }


class PlanCompletionReporter:
    """计划全部完成后经主动输出通道汇报结果（BTL-03 续跑闭环）。"""

    def __init__(
        self,
        plan_service: ActionPlanService,
        *,
        deliver: ProactiveDeliver | None = None,
    ) -> None:
        self._plan_service = plan_service
        self._deliver = deliver
        self._background: set[asyncio.Task[None]] = set()

    def set_deliver(self, deliver: ProactiveDeliver) -> None:
        """main 装配后期注入主动汇报通道。"""
        self._deliver = deliver

    def on_plan_completed(self, plan_id: UUID, user_id: UUID) -> None:
        task = asyncio.create_task(
            self._report(plan_id, user_id), name=f"aria-plan-report-{plan_id}"
        )
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def drain(self) -> None:
        """等待后台汇报任务完成；测试断言与优雅停机使用。"""
        if self._background:
            await asyncio.gather(*self._background)

    async def _report(self, plan_id: UUID, user_id: UUID) -> None:
        if self._deliver is None:
            return
        try:
            view = await self._plan_service.get_plan(user_id=user_id, plan_id=plan_id)
            if view is None or view.status != "completed":
                return
            title = view.title or "未命名计划"
            steps = "、".join(step.action_id for step in view.steps[:MAX_STEPS])
            text = f"计划「{title}」已执行完成（{len(view.steps)} 步全部成功）：{steps}。"
            with contextlib.suppress(Exception):
                await self._deliver(
                    text,
                    entity_id=f"plan:{plan_id}",
                    rule_id="plan.completed",
                    trigger_kind="plan.completed",
                    privacy_level=PrivacyLevel.L1,
                    target_user_id=user_id,
                )
        except Exception:
            logger.warning("plan completion report failed: %s", plan_id, exc_info=True)
