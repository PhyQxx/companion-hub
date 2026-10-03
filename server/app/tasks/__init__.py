"""Public exports resolved without importing unrelated adapters."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .models import (
        ClaimedTask,
        RepeatKind,
        TaskKind,
        TaskStatus,
        TaskTrigger,
        TaskView,
        compute_next_fire,
        validate_trigger,
    )
    from .scheduler import TRIGGER_KIND_EVENT, TRIGGER_KIND_TIME, TaskScheduler
    from .store import TaskStore
    from .tools import ReminderCancelTool, ReminderCreateTool, ReminderListTool

_EXPORTS = {
    "ClaimedTask": ("app.tasks.models", "ClaimedTask"),
    "RepeatKind": ("app.tasks.models", "RepeatKind"),
    "TaskKind": ("app.tasks.models", "TaskKind"),
    "TaskStatus": ("app.tasks.models", "TaskStatus"),
    "TaskTrigger": ("app.tasks.models", "TaskTrigger"),
    "TaskView": ("app.tasks.models", "TaskView"),
    "compute_next_fire": ("app.tasks.models", "compute_next_fire"),
    "validate_trigger": ("app.tasks.models", "validate_trigger"),
    "TRIGGER_KIND_EVENT": ("app.tasks.scheduler", "TRIGGER_KIND_EVENT"),
    "TRIGGER_KIND_TIME": ("app.tasks.scheduler", "TRIGGER_KIND_TIME"),
    "TaskScheduler": ("app.tasks.scheduler", "TaskScheduler"),
    "TaskStore": ("app.tasks.store", "TaskStore"),
    "ReminderCancelTool": ("app.tasks.tools", "ReminderCancelTool"),
    "ReminderCreateTool": ("app.tasks.tools", "ReminderCreateTool"),
    "ReminderListTool": ("app.tasks.tools", "ReminderListTool"),
}


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "TRIGGER_KIND_EVENT",
    "TRIGGER_KIND_TIME",
    "ClaimedTask",
    "ReminderCancelTool",
    "ReminderCreateTool",
    "ReminderListTool",
    "RepeatKind",
    "TaskKind",
    "TaskScheduler",
    "TaskStatus",
    "TaskStore",
    "TaskTrigger",
    "TaskView",
    "compute_next_fire",
    "validate_trigger",
]
