from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import case, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from app.db import Database, MeetingActionClaimRecord, MeetingRecord
from app.ids import uuid7
from app.schemas import PrivacyLevel

from .models import (
    MeetingActionItem,
    MeetingDecision,
    MeetingSummary,
    MeetingView,
    TranscriptSegment,
)


@dataclass(frozen=True, slots=True)
class ActionClaim:
    created: bool
    status: str
    task_id: UUID | None


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _to_view(record: MeetingRecord) -> MeetingView:
    return MeetingView(
        id=record.id,
        user_id=record.user_id,
        calendar_event_id=record.calendar_event_id,
        title=record.title,
        participants=list(record.participants),
        briefing=dict(record.briefing),
        privacy_level=record.privacy_level,
        status=record.status,
        consent_at=_aware(record.consent_at),
        consent_revoked_at=_aware(record.consent_revoked_at),
        transcript_segments=[
            TranscriptSegment.model_validate(item) for item in record.transcript_segments
        ],
        summary=record.summary,
        decisions=[MeetingDecision.model_validate(item) for item in record.decisions],
        action_items=[MeetingActionItem.model_validate(item) for item in record.action_items],
        started_at=_aware(record.started_at),
        ended_at=_aware(record.ended_at),
        created_at=_aware(record.created_at) or datetime.now(UTC),
        updated_at=_aware(record.updated_at) or datetime.now(UTC),
    )


class MeetingStore:
    def __init__(self, database: Database) -> None:
        self._database = database

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
    ) -> MeetingView:
        record = MeetingRecord(
            id=uuid7(),
            user_id=user_id,
            calendar_event_id=calendar_event_id,
            title=title,
            participants=participants,
            briefing=briefing,
            privacy_level=privacy_level.value,
            status="prepared",
            revision=0,
            transcript_segments=[],
            decisions=[],
            action_items=[],
            created_at=now,
            updated_at=now,
        )
        async with self._database.sessions.begin() as session:
            session.add(record)
        return _to_view(record)

    async def get(self, user_id: UUID, meeting_id: UUID) -> MeetingView:
        return _to_view(await self._record(user_id, meeting_id))

    async def list_meetings(
        self, user_id: UUID, *, limit: int = 100
    ) -> list[MeetingView]:
        query = (
            select(MeetingRecord)
            .where(MeetingRecord.user_id == user_id)
            .order_by(MeetingRecord.created_at.desc())
            .limit(limit)
        )
        async with self._database.sessions() as session:
            records = list(await session.scalars(query))
        return [_to_view(record) for record in records]

    async def authorize(self, user_id: UUID, meeting_id: UUID, now: datetime) -> MeetingView:
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                update(MeetingRecord)
                .where(
                    MeetingRecord.id == meeting_id,
                    MeetingRecord.user_id == user_id,
                    MeetingRecord.status == "prepared",
                )
                .values(
                    status="recording",
                    consent_at=now,
                    started_at=now,
                    updated_at=now,
                    revision=MeetingRecord.revision + 1,
                )
            )
        if not _changed(result):
            await self._record(user_id, meeting_id)
            raise ValueError("meeting is not awaiting transcription authorization")
        return await self.get(user_id, meeting_id)

    async def revoke(self, user_id: UUID, meeting_id: UUID, now: datetime) -> MeetingView:
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                update(MeetingRecord)
                .where(
                    MeetingRecord.id == meeting_id,
                    MeetingRecord.user_id == user_id,
                    MeetingRecord.status == "recording",
                )
                .values(
                    status="consent_revoked",
                    consent_revoked_at=now,
                    ended_at=now,
                    updated_at=now,
                    revision=MeetingRecord.revision + 1,
                )
            )
        if not _changed(result):
            await self._record(user_id, meeting_id)
            raise ValueError("meeting transcription is not active")
        return await self.get(user_id, meeting_id)

    async def cancel(self, user_id: UUID, meeting_id: UUID, now: datetime) -> MeetingView:
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                update(MeetingRecord)
                .where(
                    MeetingRecord.id == meeting_id,
                    MeetingRecord.user_id == user_id,
                    MeetingRecord.status.in_(("prepared", "recording", "consent_revoked")),
                )
                .values(
                    status="cancelled",
                    ended_at=now,
                    consent_revoked_at=case(
                        (MeetingRecord.status == "recording", now),
                        else_=MeetingRecord.consent_revoked_at,
                    ),
                    updated_at=now,
                    revision=MeetingRecord.revision + 1,
                )
            )
        if not _changed(result):
            await self._record(user_id, meeting_id)
            raise ValueError("meeting can no longer be cancelled")
        return await self.get(user_id, meeting_id)

    async def append_segments(
        self,
        user_id: UUID,
        meeting_id: UUID,
        segments: list[TranscriptSegment],
        now: datetime,
    ) -> MeetingView:
        additions = [item.model_dump(mode="json") for item in segments]
        for _ in range(4):
            record = await self._record(user_id, meeting_id)
            if record.status != "recording" or record.consent_at is None:
                raise PermissionError("transcription authorization is required")
            combined = [*record.transcript_segments, *additions]
            if len(combined) > 2_000:
                raise ValueError("meeting transcript exceeds 2000 segments")
            encoded_chars = sum(len(str(item.get("text", ""))) for item in combined)
            if encoded_chars > 1_000_000:
                raise ValueError("meeting transcript exceeds 1000000 characters")
            async with self._database.sessions.begin() as session:
                result = await session.execute(
                    update(MeetingRecord)
                    .where(
                        MeetingRecord.id == meeting_id,
                        MeetingRecord.user_id == user_id,
                        MeetingRecord.status == "recording",
                        MeetingRecord.consent_at.is_not(None),
                        MeetingRecord.revision == record.revision,
                    )
                    .values(
                        transcript_segments=combined,
                        updated_at=now,
                        revision=record.revision + 1,
                    )
                )
            if _changed(result):
                return await self.get(user_id, meeting_id)
        current = await self._record(user_id, meeting_id)
        if current.status != "recording" or current.consent_at is None:
            raise PermissionError("transcription authorization is required")
        raise RuntimeError("meeting transcript changed concurrently; retry the batch")

    async def complete(
        self,
        user_id: UUID,
        meeting_id: UUID,
        result: MeetingSummary,
        expected_segment_count: int,
        now: datetime,
    ) -> MeetingView:
        record = await self._record(user_id, meeting_id)
        if record.status != "recording" or record.consent_at is None:
            raise ValueError("only an authorized active meeting can be completed")
        if not record.transcript_segments:
            raise ValueError("meeting transcript is empty")
        if len(record.transcript_segments) != expected_segment_count:
            raise RuntimeError("meeting transcript changed; regenerate the summary")
        async with self._database.sessions.begin() as session:
            statement = (
                update(MeetingRecord)
                .where(
                    MeetingRecord.id == meeting_id,
                    MeetingRecord.user_id == user_id,
                    MeetingRecord.status == "recording",
                    MeetingRecord.consent_at.is_not(None),
                    MeetingRecord.revision == record.revision,
                )
                .values(
                    status="completed",
                    summary=result.summary,
                    decisions=[item.model_dump(mode="json") for item in result.decisions],
                    action_items=[item.model_dump(mode="json") for item in result.action_items],
                    ended_at=now,
                    updated_at=now,
                    revision=MeetingRecord.revision + 1,
                )
            )
            changed = _changed(await session.execute(statement))
        if not changed:
            record = await self._record(user_id, meeting_id)
            if record.status != "recording" or record.consent_at is None:
                raise ValueError("only an authorized active meeting can be completed")
            raise RuntimeError("meeting transcript changed; regenerate the summary")
        return await self.get(user_id, meeting_id)

    async def set_action_created(
        self,
        user_id: UUID,
        meeting_id: UUID,
        index: int,
        action: MeetingActionItem,
        now: datetime,
    ) -> MeetingView:
        for _ in range(4):
            record = await self._record(user_id, meeting_id)
            if record.status != "completed" or not 0 <= index < len(record.action_items):
                raise LookupError("meeting action item not found")
            items = list(record.action_items)
            items[index] = action.model_dump(mode="json")
            async with self._database.sessions.begin() as session:
                result = await session.execute(
                    update(MeetingRecord)
                    .where(
                        MeetingRecord.id == meeting_id,
                        MeetingRecord.user_id == user_id,
                        MeetingRecord.status == "completed",
                        MeetingRecord.revision == record.revision,
                    )
                    .values(
                        action_items=items,
                        updated_at=now,
                        revision=record.revision + 1,
                    )
                )
            if _changed(result):
                return await self.get(user_id, meeting_id)
        raise RuntimeError("meeting action items changed concurrently; retry confirmation")

    async def claim_action(
        self,
        user_id: UUID,
        meeting_id: UUID,
        index: int,
        now: datetime,
    ) -> ActionClaim:
        record = MeetingActionClaimRecord(
            id=uuid7(),
            meeting_id=meeting_id,
            user_id=user_id,
            action_index=index,
            status="creating",
            created_at=now,
            updated_at=now,
        )
        try:
            async with self._database.sessions.begin() as session:
                session.add(record)
        except IntegrityError:
            existing = await self._action_claim(user_id, meeting_id, index)
            return ActionClaim(False, existing.status, existing.task_id)
        return ActionClaim(True, "creating", None)

    async def complete_action_claim(
        self,
        user_id: UUID,
        meeting_id: UUID,
        index: int,
        task_id: UUID,
        now: datetime,
    ) -> None:
        existing = await self._action_claim(user_id, meeting_id, index)
        async with self._database.sessions.begin() as session:
            managed = await session.get(MeetingActionClaimRecord, existing.id)
            assert managed is not None
            managed.status = "created"
            managed.task_id = task_id
            managed.updated_at = now

    async def mark_action_unknown(
        self,
        user_id: UUID,
        meeting_id: UUID,
        index: int,
        now: datetime,
    ) -> None:
        existing = await self._action_claim(user_id, meeting_id, index)
        async with self._database.sessions.begin() as session:
            managed = await session.get(MeetingActionClaimRecord, existing.id)
            assert managed is not None
            if managed.status == "creating":
                managed.status = "unknown_outcome"
                managed.updated_at = now

    async def _action_claim(
        self, user_id: UUID, meeting_id: UUID, index: int
    ) -> MeetingActionClaimRecord:
        query = select(MeetingActionClaimRecord).where(
            MeetingActionClaimRecord.user_id == user_id,
            MeetingActionClaimRecord.meeting_id == meeting_id,
            MeetingActionClaimRecord.action_index == index,
        )
        async with self._database.sessions() as session:
            record = await session.scalar(query)
        if record is None:
            raise LookupError("meeting action claim not found")
        return record

    async def _record(self, user_id: UUID, meeting_id: UUID) -> MeetingRecord:
        async with self._database.sessions() as session:
            record = await session.get(MeetingRecord, meeting_id)
        if record is None or record.user_id != user_id:
            raise LookupError("meeting not found")
        return record

def _changed(result: Any) -> bool:
    return bool(cast(CursorResult[Any], result).rowcount)
