"""Immutable meeting completion input/result; no SDK or database dependencies."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.schemas.common import PrivacyLevel


@dataclass(frozen=True, slots=True)
class MeetingSummaryInput:
    meeting_id: UUID
    instruction: str
    text: str
    privacy_level: PrivacyLevel


@dataclass(frozen=True, slots=True)
class MeetingSummaryCompletion:
    text: str


class MeetingSummaryCompletionPort(Protocol):
    async def complete(self, request: MeetingSummaryInput) -> MeetingSummaryCompletion: ...
