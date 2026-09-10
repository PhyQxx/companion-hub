from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from app.schemas.common import StrictModel


class TranscriptSegment(StrictModel):
    speaker: Annotated[str, Field(min_length=1, max_length=120)]
    text: Annotated[str, Field(min_length=1, max_length=4_000)]
    started_at: datetime | None = None


class MeetingDecision(StrictModel):
    text: Annotated[str, Field(min_length=1, max_length=500)]
    evidence: Annotated[str, Field(min_length=1, max_length=500)] | None = None


class MeetingActionItem(StrictModel):
    title: Annotated[str, Field(min_length=1, max_length=320)]
    owner: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    due_at: datetime | None = None
    evidence: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    status: Literal["proposed", "created"] = "proposed"
    task_id: UUID | None = None


class MeetingSummary(StrictModel):
    summary: Annotated[str, Field(min_length=1, max_length=8_000)]
    decisions: Annotated[list[MeetingDecision], Field(max_length=50)] = Field(
        default_factory=list
    )
    action_items: Annotated[list[MeetingActionItem], Field(max_length=100)] = Field(
        default_factory=list
    )


class MeetingView(StrictModel):
    id: UUID
    user_id: UUID
    calendar_event_id: UUID | None = None
    title: str
    participants: list[str]
    briefing: dict[str, object]
    privacy_level: Literal["L1", "L2"]
    status: Literal["prepared", "recording", "consent_revoked", "completed", "cancelled"]
    consent_at: datetime | None = None
    consent_revoked_at: datetime | None = None
    transcript_segments: list[TranscriptSegment]
    summary: str | None = None
    decisions: list[MeetingDecision]
    action_items: list[MeetingActionItem]
    started_at: datetime | None = None
    ended_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
