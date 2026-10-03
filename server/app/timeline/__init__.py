"""Public timeline exports; implementations load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .browser_recall import (
        BrowserActivityRecallResult,
        BrowserActivityRecallService,
        BrowserActivitySegment,
        has_browser_activity_intent,
    )
    from .models import (
        HistoryRecallResult,
        RecallMode,
        RecallPlan,
        TemporalRange,
        TimelineActor,
        TimelineEvent,
        TimelineEvidence,
        TimelineSearchResult,
        TimelineSourceType,
    )
    from .recall import HistoryRecallService, TemporalQueryParser, has_history_intent
    from .screen_recall import (
        ScreenActivityRecallResult,
        ScreenActivityRecallService,
        ScreenActivitySegment,
        has_screen_activity_intent,
    )
    from .store import TimelineStore, timeline_record_from_event

_EXPORTS = {
    "BrowserActivityRecallResult": ("app.timeline.browser_recall", "BrowserActivityRecallResult"),
    "BrowserActivityRecallService": ("app.timeline.browser_recall", "BrowserActivityRecallService"),
    "BrowserActivitySegment": ("app.timeline.browser_recall", "BrowserActivitySegment"),
    "HistoryRecallResult": ("app.timeline.models", "HistoryRecallResult"),
    "HistoryRecallService": ("app.timeline.recall", "HistoryRecallService"),
    "RecallMode": ("app.timeline.models", "RecallMode"),
    "RecallPlan": ("app.timeline.models", "RecallPlan"),
    "ScreenActivityRecallResult": ("app.timeline.screen_recall", "ScreenActivityRecallResult"),
    "ScreenActivityRecallService": ("app.timeline.screen_recall", "ScreenActivityRecallService"),
    "ScreenActivitySegment": ("app.timeline.screen_recall", "ScreenActivitySegment"),
    "TemporalQueryParser": ("app.timeline.recall", "TemporalQueryParser"),
    "TemporalRange": ("app.timeline.models", "TemporalRange"),
    "TimelineActor": ("app.timeline.models", "TimelineActor"),
    "TimelineEvent": ("app.timeline.models", "TimelineEvent"),
    "TimelineEvidence": ("app.timeline.models", "TimelineEvidence"),
    "TimelineSearchResult": ("app.timeline.models", "TimelineSearchResult"),
    "TimelineSourceType": ("app.timeline.models", "TimelineSourceType"),
    "TimelineStore": ("app.timeline.store", "TimelineStore"),
    "has_browser_activity_intent": ("app.timeline.browser_recall", "has_browser_activity_intent"),
    "has_history_intent": ("app.timeline.recall", "has_history_intent"),
    "has_screen_activity_intent": ("app.timeline.screen_recall", "has_screen_activity_intent"),
    "timeline_record_from_event": ("app.timeline.store", "timeline_record_from_event"),
}

__all__ = [
    "BrowserActivityRecallResult",
    "BrowserActivityRecallService",
    "BrowserActivitySegment",
    "HistoryRecallResult",
    "HistoryRecallService",
    "RecallMode",
    "RecallPlan",
    "ScreenActivityRecallResult",
    "ScreenActivityRecallService",
    "ScreenActivitySegment",
    "TemporalQueryParser",
    "TemporalRange",
    "TimelineActor",
    "TimelineEvent",
    "TimelineEvidence",
    "TimelineSearchResult",
    "TimelineSourceType",
    "TimelineStore",
    "has_browser_activity_intent",
    "has_history_intent",
    "has_screen_activity_intent",
    "timeline_record_from_event",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
