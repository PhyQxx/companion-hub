"""Detached timeline queries used by history and activity policies."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.schemas.common import PrivacyLevel

from .models import (
    TimelineActor,
    TimelineEvent,
    TimelineEvidence,
    TimelineSearchResult,
    TimelineSourceType,
)


class TimelineSearchRepository(Protocol):
    async def search(
        self,
        *,
        user_id: UUID,
        query: str = "",
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        actors: Sequence[TimelineActor] | None = None,
        source_types: Sequence[TimelineSourceType] | None = None,
        event_types: Sequence[str] | None = None,
        conversation_id: UUID | None = None,
        privacy_levels: Sequence[PrivacyLevel] = (PrivacyLevel.L0, PrivacyLevel.L1),
        limit: int = 20,
        offset: int = 0,
        candidate_limit: int = 300,
    ) -> TimelineSearchResult: ...


class TimelineRecallRepository(TimelineSearchRepository, Protocol):
    async def expand_sources(
        self,
        events: Sequence[TimelineEvent],
        *,
        user_id: UUID,
        privacy_level: PrivacyLevel,
        limit: int = 8,
    ) -> tuple[TimelineEvidence, ...]: ...
