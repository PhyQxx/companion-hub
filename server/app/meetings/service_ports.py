"""Detached meeting coordination capabilities; no persistence or model SDKs."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.calendar.models import CalendarEventView
from app.schemas.common import PrivacyLevel
from app.tasks.models import TaskKind, TaskTrigger, TaskView

from .models import ActionClaim, MeetingActionItem, MeetingSummary, MeetingView, TranscriptSegment


class MeetingRepository(Protocol):
    async def create(
        self,
        *,
        user_id: UUID,
        title: str,
        participants: list[str],
        briefing: dict[str, object],
        privacy_level: PrivacyLevel,
        calendar_event_id: UUID | None,
        now: datetime,
    ) -> MeetingView: ...

    async def get(self, user_id: UUID, meeting_id: UUID) -> MeetingView: ...

    async def list_meetings(self, user_id: UUID, *, limit: int = 100) -> list[MeetingView]: ...

    async def authorize(self, user_id: UUID, meeting_id: UUID, now: datetime) -> MeetingView: ...

    async def revoke(self, user_id: UUID, meeting_id: UUID, now: datetime) -> MeetingView: ...

    async def cancel(self, user_id: UUID, meeting_id: UUID, now: datetime) -> MeetingView: ...

    async def append_segments(
        self, user_id: UUID, meeting_id: UUID, segments: list[TranscriptSegment], now: datetime
    ) -> MeetingView: ...

    async def complete(
        self,
        user_id: UUID,
        meeting_id: UUID,
        result: MeetingSummary,
        expected_segment_count: int,
        now: datetime,
    ) -> MeetingView: ...

    async def set_action_created(
        self, user_id: UUID, meeting_id: UUID, index: int, action: MeetingActionItem, now: datetime
    ) -> MeetingView: ...

    async def claim_action(
        self, user_id: UUID, meeting_id: UUID, index: int, now: datetime
    ) -> ActionClaim: ...

    async def complete_action_claim(
        self, user_id: UUID, meeting_id: UUID, index: int, task_id: UUID, now: datetime
    ) -> None: ...

    async def mark_action_unknown(
        self, user_id: UUID, meeting_id: UUID, index: int, now: datetime
    ) -> None: ...


class MeetingTasks(Protocol):
    async def create(
        self,
        *,
        user_id: UUID,
        kind: TaskKind,
        title: str,
        trigger: TaskTrigger,
        notes: str | None = None,
        privacy_level: PrivacyLevel = PrivacyLevel.L1,
        source: str = "manual",
        source_ref: str | None = None,
        now: datetime | None = None,
    ) -> TaskView: ...

    async def get_task(self, user_id: UUID, task_id: UUID) -> TaskView: ...

    async def find_by_source_ref(self, user_id: UUID, source_ref: str) -> TaskView | None: ...


class MeetingCalendarReader(Protocol):
    async def get_event(self, user_id: UUID, event_id: UUID) -> CalendarEventView: ...


class OwnedMeetingSummarizer(Protocol):
    async def summarize(
        self,
        *,
        user_id: UUID,
        meeting_id: UUID,
        title: str,
        segments: list[TranscriptSegment],
        privacy_level: PrivacyLevel,
    ) -> MeetingSummary: ...
