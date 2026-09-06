"""FLOW-01 可复用流程契约：步骤、视图与保存前预览。

红线（docs/39 J5「保存前展示全部动作与权限」）：preview 返回每一步
的动作标签、风险等级、确认策略与参数——模型必须把它完整复述给用户，
用户明确同意后才能落库。步骤只允许引用 Action Registry 已注册动作。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field, JsonValue

from app.schemas.common import StrictModel, TokenName


class WorkflowStep(StrictModel):
    action_id: TokenName
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class WorkflowStepDetail(WorkflowStep):
    """预览用：带注册表元数据，让用户在保存前看到动作与权限。"""

    label: str
    risk: str
    confirmation_policy: str
    reversible: bool


class WorkflowView(StrictModel):
    id: UUID
    user_id: UUID
    name: str
    description: str | None = None
    steps: list[WorkflowStep] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class WorkflowPreview(StrictModel):
    """保存前展示：编译后的步骤 + 每步权限元数据，纯读不落库。"""

    name: str
    description: str | None = None
    steps: list[WorkflowStepDetail] = Field(default_factory=list)


class WorkflowRunView(StrictModel):
    """运行结果：展开成的计划（沿用计划确认/执行流）。"""

    workflow_id: UUID
    plan_id: UUID
    plan_status: str
    awaiting_confirmation: bool
