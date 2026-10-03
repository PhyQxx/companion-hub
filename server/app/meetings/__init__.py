"""Public meetings exports; implementations load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .models import (
        MeetingActionItem,
        MeetingDecision,
        MeetingSummary,
        MeetingView,
        TranscriptSegment,
    )
    from .service import MeetingService
    from .store import MeetingStore
    from .summarizer import LlmMeetingSummarizer, MeetingSummarizer, RuleBasedMeetingSummarizer
    from .summary_core import StructuredMeetingSummarizer

_EXPORTS = {
    "LlmMeetingSummarizer": ("app.meetings.summarizer", "LlmMeetingSummarizer"),
    "MeetingActionItem": ("app.meetings.models", "MeetingActionItem"),
    "MeetingDecision": ("app.meetings.models", "MeetingDecision"),
    "MeetingService": ("app.meetings.service", "MeetingService"),
    "MeetingStore": ("app.meetings.store", "MeetingStore"),
    "MeetingSummarizer": ("app.meetings.summary_core", "MeetingSummarizer"),
    "MeetingSummary": ("app.meetings.models", "MeetingSummary"),
    "MeetingView": ("app.meetings.models", "MeetingView"),
    "RuleBasedMeetingSummarizer": ("app.meetings.summary_core", "RuleBasedMeetingSummarizer"),
    "StructuredMeetingSummarizer": ("app.meetings.summary_core", "StructuredMeetingSummarizer"),
    "TranscriptSegment": ("app.meetings.models", "TranscriptSegment"),
}

__all__ = [
    "LlmMeetingSummarizer",
    "MeetingActionItem",
    "MeetingDecision",
    "MeetingService",
    "MeetingStore",
    "MeetingSummarizer",
    "MeetingSummary",
    "MeetingView",
    "RuleBasedMeetingSummarizer",
    "StructuredMeetingSummarizer",
    "TranscriptSegment",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
