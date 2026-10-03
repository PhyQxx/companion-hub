"""Perception contracts exchange immutable audit metadata, never ORM records."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypeVar
from uuid import UUID

from app.cognition.models import SemanticEvent

from .models import PerceptionDisposition, ProactivePolicySettings

T = TypeVar("T")


@dataclass(frozen=True)
class EventAuditSnapshot:
    event_id: UUID
    user_id: UUID
    kind: str
    source_kind: str
    dedupe_key: str
    privacy_level: str
    evidence_ids: tuple[str, ...]
    occurred_at: datetime
    expires_at: datetime | None


class EventAuditRepository(Protocol):
    async def get(self, event_id: UUID) -> EventAuditSnapshot | None: ...

    async def recent_duplicate(
        self,
        event: SemanticEvent,
        *,
        dedupe_key: str,
        now: datetime,
        window_seconds: int,
    ) -> EventAuditSnapshot | None: ...

    async def record(
        self,
        event: SemanticEvent,
        *,
        dedupe_key: str,
        disposition: PerceptionDisposition,
        now: datetime,
        reason_code: str | None = None,
        decision_id: UUID | None = None,
        merged_into_event_id: UUID | None = None,
    ) -> None: ...


class EventPolicy(Protocol):
    @property
    def settings(self) -> ProactivePolicySettings: ...

    async def reject_reason(self, event: SemanticEvent, *, now: datetime) -> str | None: ...


class EventAdmissionPort(Protocol):
    async def verify(self, event: SemanticEvent) -> None: ...

    async def execute(self, event: SemanticEvent, invoke: Callable[[], Awaitable[T]]) -> T: ...
