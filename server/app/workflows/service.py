"""FLOW-01 流程服务：保存前编译校验、权限预览与展开为 ActionPlan。

保存与运行都经 ActionRegistry 重新编译：动作被移除、参数 Schema 漂移
或参数非法时显式失败，绝不带病执行历史模板。运行 = 按 ACT-02 契约
创建待确认计划，确认策略（A1 预授权/A2 每次确认）由计划层逐步裁决。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from app.cognition.action_plan import ActionInvocation, ActionPlanService
from app.cognition.action_registry import ActionRegistry

from .models import (
    WorkflowPreview,
    WorkflowRunView,
    WorkflowStep,
    WorkflowStepDetail,
    WorkflowView,
)
from .store import MAX_WORKFLOW_STEPS, WorkflowStore


class WorkflowService:
    def __init__(
        self,
        store: WorkflowStore,
        registry: ActionRegistry,
        plan_service: Callable[[], ActionPlanService] | ActionPlanService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        # 计划服务在 main 中晚于流程服务完成 runner 注入，用惰性获取解耦。
        self._plan_service = plan_service
        self._clock = clock or (lambda: datetime.now(UTC))

    async def preview(
        self,
        *,
        name: str,
        description: str | None,
        steps: list[WorkflowStep],
    ) -> WorkflowPreview:
        """保存前展示：逐动作编译并附权限元数据，纯读不落库。"""
        details = self._compile_steps(steps)
        return WorkflowPreview(
            name=name.strip(),
            description=description,
            steps=details,
        )

    async def save_workflow(
        self,
        *,
        user_id: UUID,
        name: str,
        steps: list[WorkflowStep],
        description: str | None = None,
    ) -> WorkflowView:
        self._compile_steps(steps)  # 保存前再次编译校验
        return await self._store.create_workflow(
            user_id=user_id,
            name=name,
            steps=steps,
            description=description,
            now=self._clock(),
        )

    async def run_workflow(
        self,
        user_id: UUID,
        workflow_id: UUID,
        *,
        idempotency_key: str | None = None,
    ) -> WorkflowRunView:
        view = await self._store.get_workflow_view(user_id, workflow_id)
        invocations = [
            ActionInvocation(action_id=step.action_id, arguments=step.arguments)
            for step in view.steps
        ]
        plan = await self._plans().create_plan(
            user_id=user_id,
            invocations=invocations,
            title=f"流程：{view.name}",
            idempotency_key=idempotency_key,
        )
        return WorkflowRunView(
            workflow_id=view.id,
            plan_id=plan.id,
            plan_status=plan.status,
            awaiting_confirmation=plan.status == "awaiting_confirmation",
        )

    async def list_workflows(self, user_id: UUID, *, limit: int = 100) -> list[WorkflowView]:
        return await self._store.list_workflows(user_id, limit=limit)

    async def get_workflow(self, user_id: UUID, workflow_id: UUID) -> WorkflowView:
        return await self._store.get_workflow_view(user_id, workflow_id)

    async def find_by_name(self, user_id: UUID, name: str) -> WorkflowView | None:
        return await self._store.find_by_name(user_id, name)

    async def delete_workflow(self, user_id: UUID, workflow_id: UUID) -> None:
        await self._store.delete_workflow(user_id, workflow_id)

    def _compile_steps(self, steps: list[WorkflowStep]) -> list[WorkflowStepDetail]:
        if not 1 <= len(steps) <= MAX_WORKFLOW_STEPS:
            raise ValueError(f"流程需要 1-{MAX_WORKFLOW_STEPS} 个步骤")
        details: list[WorkflowStepDetail] = []
        for step in steps:
            # 未知动作/参数漂移/风险禁止（A3）都在 compile 内显式拒绝；
            # 注册表异常归一为 ValueError，调用方（工具/API）统一按 422 处理。
            try:
                compiled = self._registry.compile(step.action_id, dict(step.arguments))
            except LookupError as error:
                raise ValueError(f"未知动作：{step.action_id}") from error
            except PermissionError as error:
                raise ValueError(f"动作被安全策略禁止：{step.action_id}") from error
            definition = compiled.definition
            details.append(
                WorkflowStepDetail(
                    action_id=step.action_id,
                    arguments=step.arguments,
                    label=definition.label,
                    risk=str(definition.risk),
                    confirmation_policy=str(definition.confirmation_policy),
                    reversible=definition.reversible,
                )
            )
        return details

    def _plans(self) -> ActionPlanService:
        return self._plan_service() if callable(self._plan_service) else self._plan_service
