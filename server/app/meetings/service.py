"""Compatibility composition for meeting persistence and owned model execution."""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from app.calendar.models import CalendarEventView
from app.calendar.store import CalendarStore, _to_view
from app.runs.completion import model_owner
from app.schemas.common import PrivacyLevel
from app.tasks.store import TaskStore

from .models import MeetingSummary, TranscriptSegment
from .service_core import MeetingCoordinator
from .service_core import _normalize_participants as _normalize_participants
from .store import MeetingStore
from .summary_core import MeetingSummarizer


class _CalendarReader:
    def __init__(self, store: CalendarStore) -> None:
        self._store = store

    async def get_event(self, user_id: UUID, event_id: UUID) -> CalendarEventView:
        return _to_view(await self._store.get_event(user_id, event_id))


class _OwnedSummarizer:
    def __init__(self, summarizer: MeetingSummarizer) -> None:
        self._summarizer = summarizer

    async def summarize(
        self,
        *,
        user_id: UUID,
        meeting_id: UUID,
        title: str,
        segments: list[TranscriptSegment],
        privacy_level: PrivacyLevel,
    ) -> MeetingSummary:
        with model_owner(user_id):
            return await self._summarizer.summarize(
                meeting_id=meeting_id,
                title=title,
                segments=segments,
                privacy_level=privacy_level,
            )


class MeetingService(MeetingCoordinator):
    def __init__(
        self,
        store: MeetingStore,
        calendar_store: CalendarStore,
        task_store: TaskStore,
        summarizer: MeetingSummarizer,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            store,
            _CalendarReader(calendar_store),
            task_store,
            _OwnedSummarizer(summarizer),
            clock=clock,
        )
