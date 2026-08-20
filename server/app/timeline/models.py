from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class TimelineSourceType(StrEnum):
    MESSAGE = "message"
    EVENT = "event"
    DEVICE = "device"
    TOOL = "tool"
    CALENDAR = "calendar"
    SYSTEM = "system"


class TimelineActor(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    DEVICE = "device"
    SYSTEM = "system"
    EXTERNAL = "external"


class RecallMode(StrEnum):
    WORKING = "working"
    MEMORY = "memory"
    TIMELINE = "timeline"
    SOURCE = "source"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    id: int
    user_id: UUID
    occurred_at: datetime
    ended_at: datetime | None
    source_type: str
    source_id: str
    actor: str
    event_type: str
    conversation_id: UUID | None
    title: str | None
    summary: str
    privacy_level: str
    importance: float
    entities: tuple[dict[str, object], ...]
    keywords: tuple[str, ...]
    metadata: dict[str, object]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TimelineEvidence:
    timeline_id: int
    source_type: str
    source_id: str
    occurred_at: datetime
    actor: str
    event_type: str
    text: str


@dataclass(frozen=True, slots=True)
class TimelineSearchResult:
    events: tuple[TimelineEvent, ...]
    candidate_count: int


@dataclass(frozen=True, slots=True)
class TemporalRange:
    start_at: datetime
    end_at: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class RecallPlan:
    query: str
    start_at: datetime | None
    end_at: datetime | None
    actors: tuple[TimelineActor, ...] = ()
    source_types: tuple[TimelineSourceType, ...] = ()
    event_types: tuple[str, ...] = ()
    conversation_id: UUID | None = None
    max_results: int = 8


@dataclass(frozen=True, slots=True)
class HistoryRecallResult:
    mode: RecallMode
    plan: RecallPlan
    events: tuple[TimelineEvent, ...]
    evidence: tuple[TimelineEvidence, ...]
    search_count: int
    candidate_count: int
