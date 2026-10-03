"""Detached inputs and repository ports for commitment recognition."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.schemas import PrivacyLevel

from .models import GoalKind, GoalView


@dataclass(frozen=True, slots=True)
class CommitmentInput:
    message_id: UUID
    text: str
    privacy_level: PrivacyLevel


@dataclass(frozen=True, slots=True)
class CommitmentCompletion:
    text: str


class CommitmentCompletionPort(Protocol):
    async def complete(self, request: CommitmentInput) -> CommitmentCompletion: ...


class CommitmentRepository(Protocol):
    async def goal_by_source(
        self, user_id: UUID, *, source_kind: str, source_id: str
    ) -> GoalView | None: ...

    async def create_goal(
        self,
        *,
        user_id: UUID,
        kind: GoalKind,
        title: str,
        source_kind: str,
        source_id: str,
        due_at: datetime | None = None,
        privacy_level: PrivacyLevel | None = None,
    ) -> GoalView: ...
