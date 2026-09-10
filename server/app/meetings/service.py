from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from app.calendar import CalendarParticipant, CalendarStore
from app.schemas import PrivacyLevel
from app.tasks import TaskKind, TaskStore, TaskTrigger

from .models import MeetingActionItem, MeetingView, TranscriptSegment
from .store import MeetingStore
from .summarizer import MeetingSummarizer


class MeetingService:
    def __init__(
        self,
        store: MeetingStore,
        calendar_store: CalendarStore,
        task_store: TaskStore,
        summarizer: MeetingSummarizer,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._calendar = calendar_store
        self._tasks = task_store
        self._summarizer = summarizer
        self._clock = clock or (lambda: datetime.now(UTC))
        self._action_locks: dict[tuple[UUID, int], asyncio.Lock] = {}

    async def prepare(
        self,
        user_id: UUID,
        *,
        title: str | None,
        participants: list[str],
        privacy_level: PrivacyLevel,
        calendar_event_id: UUID | None = None,
    ) -> MeetingView:
        if privacy_level not in {PrivacyLevel.L1, PrivacyLevel.L2}:
            raise ValueError("meeting privacy level must be L1 or L2")
        briefing: dict[str, object] = {}
        resolved_title = (title or "").strip()
        resolved_participants = _normalize_participants(participants)
        if calendar_event_id is not None:
            event = await self._calendar.get_event(user_id, calendar_event_id)
            resolved_title = resolved_title or event.title
            if not resolved_participants:
                resolved_participants = _normalize_participants(
                    [CalendarParticipant.model_validate(item).name for item in event.participants]
                )
            briefing = {
                "calendar_event_id": str(event.id),
                "starts_at": event.starts_at.isoformat(),
                "ends_at": event.ends_at.isoformat(),
                "location": event.location,
                "notes": event.notes,
                "participants": list(event.participants),
            }
        if not resolved_title:
            raise ValueError("meeting title is required")
        return await self._store.create(
            user_id=user_id,
            title=resolved_title,
            participants=resolved_participants,
            briefing=briefing,
            privacy_level=privacy_level,
            calendar_event_id=calendar_event_id,
            now=self._clock(),
        )

    async def authorize_transcription(self, user_id: UUID, meeting_id: UUID) -> MeetingView:
        return await self._store.authorize(user_id, meeting_id, self._clock())

    async def revoke_transcription(self, user_id: UUID, meeting_id: UUID) -> MeetingView:
        return await self._store.revoke(user_id, meeting_id, self._clock())

    async def cancel(self, user_id: UUID, meeting_id: UUID) -> MeetingView:
        return await self._store.cancel(user_id, meeting_id, self._clock())

    async def append_transcript(
        self,
        user_id: UUID,
        meeting_id: UUID,
        segments: list[TranscriptSegment],
    ) -> MeetingView:
        meeting = await self._store.get(user_id, meeting_id)
        allowed = {name.casefold() for name in meeting.participants}
        allowed.update({"我", "self"})
        for segment in segments:
            if segment.speaker.casefold() not in allowed:
                raise ValueError("speaker must be an explicitly declared participant")
        return await self._store.append_segments(
            user_id, meeting_id, segments, self._clock()
        )

    async def finish(self, user_id: UUID, meeting_id: UUID) -> MeetingView:
        meeting = await self._store.get(user_id, meeting_id)
        if meeting.status != "recording" or meeting.consent_at is None:
            raise ValueError("only an authorized active meeting can be completed")
        if not meeting.transcript_segments:
            raise ValueError("meeting transcript is empty")
        result = await self._summarizer.summarize(
            meeting_id=meeting.id,
            title=meeting.title,
            segments=meeting.transcript_segments,
            privacy_level=PrivacyLevel(meeting.privacy_level),
        )
        return await self._store.complete(
            user_id,
            meeting_id,
            result,
            len(meeting.transcript_segments),
            self._clock(),
        )

    async def confirm_action_item(
        self,
        user_id: UUID,
        meeting_id: UUID,
        index: int,
        *,
        due_at: datetime | None = None,
    ) -> MeetingView:
        key = (meeting_id, index)
        lock = self._action_locks.setdefault(key, asyncio.Lock())
        async with lock:
            meeting = await self._store.get(user_id, meeting_id)
            if meeting.status != "completed" or not 0 <= index < len(meeting.action_items):
                raise LookupError("meeting action item not found")
            item = meeting.action_items[index]
            if item.status == "created":
                return meeting
            deadline = due_at or item.due_at
            if deadline is None:
                raise ValueError("action item requires a due time before task creation")
            now = self._clock()
            if deadline <= now:
                raise ValueError("action item due time must be in the future")
            source_ref = f"meeting:{meeting_id}:action:{index}"
            existing = await self._tasks.find_by_source_ref(user_id, source_ref)
            if existing is not None:
                created = item.model_copy(
                    update={"due_at": deadline, "status": "created", "task_id": existing.id}
                )
                return await self._store.set_action_created(
                    user_id, meeting_id, index, created, now
                )
            claim = await self._store.claim_action(user_id, meeting_id, index, now)
            if not claim.created:
                if claim.task_id is not None:
                    existing = await self._tasks.get_task(user_id, claim.task_id)
                else:
                    existing = await self._tasks.find_by_source_ref(user_id, source_ref)
                if existing is None:
                    raise ValueError(f"action confirmation is {claim.status}")
                await self._store.complete_action_claim(
                    user_id, meeting_id, index, existing.id, now
                )
            else:
                try:
                    existing = await self._tasks.create(
                        user_id=user_id,
                        kind=TaskKind.TASK,
                        title=item.title,
                        notes=(
                            f"会议：{meeting.title}"
                            + (f"\n负责人：{item.owner}" if item.owner else "")
                            + (f"\n证据：{item.evidence}" if item.evidence else "")
                        ),
                        trigger=TaskTrigger(type="time", at=deadline),
                        privacy_level=PrivacyLevel(meeting.privacy_level),
                        source="meeting",
                        source_ref=source_ref,
                        now=now,
                    )
                except BaseException:
                    await asyncio.shield(
                        self._store.mark_action_unknown(
                            user_id, meeting_id, index, self._clock()
                        )
                    )
                    raise
                await self._store.complete_action_claim(
                    user_id=user_id,
                    meeting_id=meeting_id,
                    index=index,
                    task_id=existing.id,
                    now=now,
                )
            created = MeetingActionItem(
                title=item.title,
                owner=item.owner,
                due_at=deadline,
                evidence=item.evidence,
                status="created",
                task_id=existing.id,
            )
            return await self._store.set_action_created(
                user_id, meeting_id, index, created, now
            )

    async def get(self, user_id: UUID, meeting_id: UUID) -> MeetingView:
        return await self._store.get(user_id, meeting_id)

    async def list(self, user_id: UUID, *, limit: int = 100) -> list[MeetingView]:
        return await self._store.list_meetings(user_id, limit=limit)


def _normalize_participants(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    if len(result) > 100:
        raise ValueError("meeting supports at most 100 participants")
    return result
