"""Detached operational facts consumed by world assembly."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.harness.context import ContextReference
from app.schemas import PrivacyLevel


@dataclass(frozen=True, slots=True)
class WorldFacts:
    timezone: str | None
    last_interaction_at: datetime | None
    active_capabilities: tuple[str, ...]
    recent_proactive_count: int
    same_trigger_recent_count: int
    ignored_same_trigger_count: int


class WorldFactsRepository(Protocol):
    async def read(
        self, *, user_id: UUID, trigger_kind: str, privacy_level: PrivacyLevel, now: datetime
    ) -> WorldFacts: ...

    async def attach_memory_lineage(
        self, references: tuple[ContextReference, ...], *, user_id: UUID
    ) -> tuple[ContextReference, ...]: ...
