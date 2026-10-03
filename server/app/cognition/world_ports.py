"""Inputs to world assembly, independent of query implementations."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.memory.retrieval_models import RetrievalResult
from app.schemas import PrivacyLevel
from app.timeline.models import TimelineSearchResult

from .models import GoalView


class GoalRepository(Protocol):
    async def active_goals(
        self, user_id: UUID, *, now: datetime, max_privacy_level: PrivacyLevel | None = None
    ) -> list[GoalView]: ...


class MemoryQuerySource(Protocol):
    async def retrieve(
        self, query: str, *, user_id: UUID, privacy_level: PrivacyLevel, now: datetime | None = None
    ) -> RetrievalResult: ...


class TimelineQuerySource(Protocol):
    async def search(
        self,
        *,
        user_id: UUID,
        query: str = "",
        privacy_levels: Sequence[PrivacyLevel] = (PrivacyLevel.L0, PrivacyLevel.L1),
        limit: int = 20,
    ) -> TimelineSearchResult: ...
