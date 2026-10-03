"""Meeting orchestration must own transcript inputs across suspended work."""

import asyncio
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest

from app.meetings.models import MeetingSummary, MeetingView, TranscriptSegment
from app.meetings.service_core import MeetingCoordinator
from app.meetings.service_ports import MeetingCalendarReader, MeetingRepository, MeetingTasks
from app.schemas.common import PrivacyLevel

NOW = datetime(2026, 10, 3, tzinfo=UTC)


class Repository:
    def __init__(self) -> None:
        self.owner, self.id = uuid4(), uuid4()
        self.meeting = MeetingView(
            id=self.id,
            user_id=self.owner,
            title="Synthetic meeting",
            participants=["self"],
            briefing={},
            privacy_level="L1",
            status="recording",
            consent_at=NOW,
            transcript_segments=[TranscriptSegment(speaker="self", text="Original transcript")],
            decisions=[],
            action_items=[],
            created_at=NOW,
            updated_at=NOW,
        )
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.wait = False
        self.appended: list[TranscriptSegment] = []
        self.completed_count: int | None = None
        self.created: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> MeetingView:
        self.created.append(kwargs)
        return self.meeting

    async def get(self, user_id: object, meeting_id: object) -> MeetingView:
        self.entered.set()
        if self.wait:
            await self.release.wait()
        return self.meeting

    async def append_segments(
        self,
        user_id: object,
        meeting_id: object,
        segments: list[TranscriptSegment],
        now: datetime,
    ) -> MeetingView:
        self.appended = list(segments)
        return self.meeting

    async def complete(
        self,
        user_id: object,
        meeting_id: object,
        result: MeetingSummary,
        expected_segment_count: int,
        now: datetime,
    ) -> MeetingView:
        self.completed_count = expected_segment_count
        return self.meeting


class Summarizer:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self.calls: list[dict[str, Any]] = []
        self.change = False

    async def summarize(self, **kwargs: Any) -> MeetingSummary:
        self.calls.append(kwargs)
        if self.change:
            self.repository.meeting.transcript_segments.clear()
        return MeetingSummary(summary="Synthetic summary")


def service(repository: Repository, summarizer: Summarizer) -> MeetingCoordinator:
    return MeetingCoordinator(
        cast(MeetingRepository, repository),
        cast(MeetingCalendarReader, None),
        cast(MeetingTasks, None),
        summarizer,
        clock=lambda: NOW,
    )


async def test_append_owns_segments_before_waiting_for_meeting() -> None:
    repository = Repository()
    repository.wait = True
    segments = [TranscriptSegment(speaker="self", text="Accepted transcript")]
    task = asyncio.create_task(
        service(repository, Summarizer(repository)).append_transcript(
            repository.owner,
            repository.id,
            segments,
        )
    )
    try:
        await asyncio.wait_for(repository.entered.wait(), 3)
        segments.clear()
        repository.release.set()
        await asyncio.wait_for(task, 3)
        assert [segment.text for segment in repository.appended] == ["Accepted transcript"]
    finally:
        repository.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_finish_uses_original_transcript_count_after_model_wait() -> None:
    repository = Repository()
    summarizer = Summarizer(repository)
    summarizer.change = True
    await service(repository, summarizer).finish(repository.owner, repository.id)
    assert repository.completed_count == 1
    assert len(summarizer.calls[0]["segments"]) == 1


@pytest.mark.parametrize("change", ["owner", "id"])
async def test_finish_rechecks_owned_meeting_before_model_call(change: str) -> None:
    repository = Repository()
    repository.meeting = repository.meeting.model_copy(
        update={
            "user_id" if change == "owner" else "id": uuid4(),
        }
    )
    summarizer = Summarizer(repository)
    with pytest.raises(LookupError):
        await service(repository, summarizer).finish(repository.owner, repository.id)
    assert summarizer.calls == []
    assert repository.completed_count is None


@pytest.mark.parametrize("privacy", ["L0", "L3"])
async def test_finish_rejects_invalid_meeting_privacy_before_summary(privacy: str) -> None:
    repository = Repository()
    repository.meeting = repository.meeting.model_copy(update={"privacy_level": privacy})
    summarizer = Summarizer(repository)
    with pytest.raises(ValueError, match="privacy"):
        await service(repository, summarizer).finish(repository.owner, repository.id)
    assert summarizer.calls == [] and repository.completed_count is None


@pytest.mark.parametrize("change", ["owner", "id", "cancelled"])
async def test_prepare_rechecks_calendar_source_before_create(change: str) -> None:
    from app.calendar.models import CalendarEventView

    repository = Repository()
    expected = uuid4()

    class Calendar:
        async def get_event(self, user_id: object, event_id: object) -> CalendarEventView:
            return CalendarEventView(
                id=uuid4() if change == "id" else expected,
                user_id=uuid4() if change == "owner" else repository.owner,
                calendar_id="primary",
                title="Synthetic calendar source",
                starts_at=NOW,
                ends_at=NOW,
                status="cancelled" if change == "cancelled" else "active",
            )

    coordinator = MeetingCoordinator(
        cast(MeetingRepository, repository),
        Calendar(),
        cast(MeetingTasks, None),
        Summarizer(repository),
        clock=lambda: NOW,
    )
    with pytest.raises(ValueError if change == "cancelled" else LookupError):
        await coordinator.prepare(
            repository.owner,
            title=None,
            participants=[],
            privacy_level=PrivacyLevel.L1,
            calendar_event_id=expected,
        )
    assert repository.created == []
