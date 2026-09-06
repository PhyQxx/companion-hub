"""FLOW-01 聊天端流程工具：两阶段保存与运行。

红线（docs/39 J5「保存前展示全部动作与权限」）：workflow_save 首次调用
只做编译校验并返回每一步的动作标签、风险等级、确认策略与参数，模型把
预览完整复述给用户，用户明确同意后带 confirmed=true 才落库；同 turn
幂等。workflow_run 展开为待确认计划，A2 步骤必须经计划确认流执行。
两者仅 L1 挂载（L0 不写个人数据、L2 内容不入库，执行层兜底拒绝）。
"""

from __future__ import annotations

from collections import OrderedDict
from time import perf_counter
from typing import Annotated, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.llm import ToolDefinition
from app.schemas.common import PrivacyLevel
from app.tools.contracts import ToolContext, ToolResult

from .models import WorkflowStep, WorkflowView
from .service import WorkflowService


class WorkflowSaveArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[str, Field(min_length=1, max_length=120)]
    description: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    steps: Annotated[list[WorkflowStep], Field(min_length=1, max_length=10)]
    confirmed: bool = False


class WorkflowRunArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)

    @property
    def has_target(self) -> bool:
        return self.workflow_id is not None or self.name is not None


class WorkflowSaveTool:
    name = "workflow_save"
    description = (
        "把一组已注册动作保存为可复用流程（1-10 步）。首次调用不要传 confirmed："
        "只做编译校验并返回每一步的动作名称、风险等级、确认策略与参数，把这些"
        "完整复述给用户；用户明确同意后再带 confirmed=true 保存。只允许引用"
        "动作目录（/api/v1/cognition/actions/catalog）里已注册的动作。"
    )
    arguments_model: type[BaseModel] = WorkflowSaveArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    def __init__(self, service: WorkflowService) -> None:
        self._service = service
        self._saved_by_turn: OrderedDict[UUID, str] = OrderedDict()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=WorkflowSaveArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(WorkflowSaveArgs, arguments)
        # ToolContext 经 pydantic use_enum_values 校验后是普通字符串，必须用 == 比较
        if context.privacy_level == PrivacyLevel.L2:
            return self._failure("private_session_unsupported", started)
        if context.turn_id is None or context.user_id is None:
            return self._failure("idempotency_key_missing", started)
        try:
            preview = await self._service.preview(
                name=args.name,
                description=args.description,
                steps=list(args.steps),
            )
        except ValueError as error:
            return self._invalid(str(error), started)
        if not args.confirmed:
            return ToolResult(
                ok=True,
                tool_name=self.name,
                data={
                    "saved": False,
                    "confirmation_required": True,
                    "preview": preview.model_dump(mode="json"),
                },
                latency_ms=(perf_counter() - started) * 1_000,
            )
        saved_turn = self._saved_by_turn.get(context.turn_id)
        if saved_turn is not None:
            existing = await self._service.find_by_name(context.user_id, saved_turn)
            if existing is not None:
                return ToolResult(
                    ok=True,
                    tool_name=self.name,
                    data={
                        "saved": True,
                        "duplicate": True,
                        "workflow": _workflow_payload(existing),
                    },
                    latency_ms=(perf_counter() - started) * 1_000,
                )
        existing = await self._service.find_by_name(context.user_id, args.name)
        if existing is not None:
            return self._invalid(f"同名流程已存在：{existing.name}", started)
        try:
            view = await self._service.save_workflow(
                user_id=context.user_id,
                name=args.name,
                description=args.description,
                steps=list(args.steps),
            )
        except ValueError as error:
            return self._invalid(str(error), started)
        self._remember(context.turn_id, view.name)
        return ToolResult(
            ok=True,
            tool_name=self.name,
            data={"saved": True, "workflow": _workflow_payload(view)},
            latency_ms=(perf_counter() - started) * 1_000,
        )

    def _remember(self, turn_id: UUID, name: str) -> None:
        self._saved_by_turn[turn_id] = name
        while len(self._saved_by_turn) > 128:
            self._saved_by_turn.popitem(last=False)

    def _invalid(self, detail: str, started: float) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=self.name,
            reason_code="invalid_workflow",
            data={"saved": False, "reason_detail": detail},
            latency_ms=(perf_counter() - started) * 1_000,
        )

    def _failure(self, reason: str, started: float) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=self.name,
            reason_code=reason,
            latency_ms=(perf_counter() - started) * 1_000,
        )


class WorkflowRunTool:
    name = "workflow_run"
    description = (
        "运行一个已保存的流程：展开为动作计划并返回计划 ID 与每步确认要求。"
        "含每次确认（A2）步骤的计划必须等用户在计划确认流里明确同意后才执行，"
        "不要替用户确认。按 ID 或名称查找流程。"
    )
    arguments_model: type[BaseModel] = WorkflowRunArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    def __init__(self, service: WorkflowService) -> None:
        self._service = service

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=WorkflowRunArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(WorkflowRunArgs, arguments)
        # ToolContext 经 pydantic use_enum_values 校验后是普通字符串，必须用 == 比较
        if context.privacy_level == PrivacyLevel.L2:
            return self._failure("private_session_unsupported", started)
        if context.user_id is None:
            return self._failure("action_user_required", started)
        if not args.has_target:
            return self._failure("workflow_target_missing", started)
        try:
            view: WorkflowView | None
            if args.workflow_id is not None:
                view = await self._service.get_workflow(context.user_id, args.workflow_id)
            else:
                view = await self._service.find_by_name(context.user_id, args.name or "")
            if view is None:
                return ToolResult(
                    ok=False,
                    tool_name=self.name,
                    reason_code="workflow_not_found",
                    latency_ms=(perf_counter() - started) * 1_000,
                )
            run = await self._service.run_workflow(
                context.user_id,
                view.id,
                idempotency_key=f"workflow-run-{context.turn_id}" if context.turn_id else None,
            )
        except LookupError:
            return ToolResult(
                ok=False,
                tool_name=self.name,
                reason_code="workflow_not_found",
                latency_ms=(perf_counter() - started) * 1_000,
            )
        except ValueError as error:
            return ToolResult(
                ok=False,
                tool_name=self.name,
                reason_code="invalid_workflow",
                data={"reason_detail": str(error)},
                latency_ms=(perf_counter() - started) * 1_000,
            )
        return ToolResult(
            ok=True,
            tool_name=self.name,
            data={
                "workflow_id": str(run.workflow_id),
                "plan_id": str(run.plan_id),
                "plan_status": run.plan_status,
                "awaiting_confirmation": run.awaiting_confirmation,
            },
            latency_ms=(perf_counter() - started) * 1_000,
        )

    def _failure(self, reason: str, started: float) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=self.name,
            reason_code=reason,
            latency_ms=(perf_counter() - started) * 1_000,
        )


def _workflow_payload(view: WorkflowView) -> dict[str, object]:
    return {
        "id": str(view.id),
        "name": view.name,
        "description": view.description,
        "steps": [
            {"action_id": step.action_id, "arguments": step.arguments}
            for step in view.steps
        ],
    }


__all__ = ["WorkflowRunTool", "WorkflowSaveTool"]
