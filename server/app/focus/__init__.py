"""FOCUS-01 专注守护，按需加载纯核心与存储适配器。"""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .analysis import (
        FocusObservation,
        FocusSession,
        FocusSignal,
        analyze_focus,
        cooldown_passed,
        with_nudge_marked,
    )
    from .core import FocusEvaluation, FocusSessionService
    from .scheduler import FocusScheduler
    from .service import FocusService
    from .tools import FocusStartTool, FocusStatusTool, FocusStopTool

_EXPORTS = {
    "FocusObservation": ("app.focus.analysis", "FocusObservation"),
    "FocusSession": ("app.focus.analysis", "FocusSession"),
    "FocusSignal": ("app.focus.analysis", "FocusSignal"),
    "analyze_focus": ("app.focus.analysis", "analyze_focus"),
    "cooldown_passed": ("app.focus.analysis", "cooldown_passed"),
    "with_nudge_marked": ("app.focus.analysis", "with_nudge_marked"),
    "FocusScheduler": ("app.focus.scheduler", "FocusScheduler"),
    "FocusService": ("app.focus.service", "FocusService"),
    "FocusStartTool": ("app.focus.tools", "FocusStartTool"),
    "FocusStatusTool": ("app.focus.tools", "FocusStatusTool"),
    "FocusStopTool": ("app.focus.tools", "FocusStopTool"),
    "FocusEvaluation": ("app.focus.core", "FocusEvaluation"),
    "FocusSessionService": ("app.focus.core", "FocusSessionService"),
}


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "FocusEvaluation",
    "FocusObservation",
    "FocusScheduler",
    "FocusService",
    "FocusSession",
    "FocusSessionService",
    "FocusSignal",
    "FocusStartTool",
    "FocusStatusTool",
    "FocusStopTool",
    "analyze_focus",
    "cooldown_passed",
    "with_nudge_marked",
]
