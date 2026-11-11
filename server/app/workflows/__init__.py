"""FLOW-01 可复用流程：已注册动作的持久化模板与计划展开。

DIST（docs/09 §4）计划轨迹蒸馏与回放晋级见 drafts 模块。
Public exports load only when requested.
"""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .drafts import PlanDistiller, WorkflowDraftStore, steps_dedupe_key
    from .models import (
        WorkflowPreview,
        WorkflowRunView,
        WorkflowStep,
        WorkflowStepDetail,
        WorkflowView,
    )
    from .service import WorkflowService
    from .store import WorkflowStore
    from .tools import WorkflowRunTool, WorkflowSaveTool

_EXPORTS = {
    "PlanDistiller": ("app.workflows.drafts", "PlanDistiller"),
    "WorkflowDraftStore": ("app.workflows.drafts", "WorkflowDraftStore"),
    "WorkflowPreview": ("app.workflows.models", "WorkflowPreview"),
    "WorkflowRunTool": ("app.workflows.tools", "WorkflowRunTool"),
    "WorkflowRunView": ("app.workflows.models", "WorkflowRunView"),
    "WorkflowSaveTool": ("app.workflows.tools", "WorkflowSaveTool"),
    "WorkflowService": ("app.workflows.service", "WorkflowService"),
    "WorkflowStep": ("app.workflows.models", "WorkflowStep"),
    "WorkflowStepDetail": ("app.workflows.models", "WorkflowStepDetail"),
    "WorkflowStore": ("app.workflows.store", "WorkflowStore"),
    "WorkflowView": ("app.workflows.models", "WorkflowView"),
    "steps_dedupe_key": ("app.workflows.drafts", "steps_dedupe_key"),
}

__all__ = [
    "PlanDistiller",
    "WorkflowDraftStore",
    "WorkflowPreview",
    "WorkflowRunTool",
    "WorkflowRunView",
    "WorkflowSaveTool",
    "WorkflowService",
    "WorkflowStep",
    "WorkflowStepDetail",
    "WorkflowStore",
    "WorkflowView",
    "steps_dedupe_key",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
