from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import Field

from app.schemas.common import PrivacyLevel, StrictModel


class MemoryType(StrEnum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PREFERENCE = "preference"
    COMMITMENT = "commitment"
    EMOTIONAL = "emotional"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    SUPERSEDED = "superseded"
    CONFLICT = "conflict"


class MemorySourceKind(StrEnum):
    MESSAGE = "message"
    EVENT = "event"
    MEMORY = "memory"
    MANUAL = "manual"


class ConsolidateDecision(StrEnum):
    CREATED = "created"
    SUPPORTED = "supported"
    CONFLICT = "conflict"
    SUPERSEDED = "superseded"


class MemorySourceRef(StrictModel):
    source_kind: MemorySourceKind
    source_id: Annotated[str, Field(min_length=1, max_length=200)]
    excerpt: Annotated[str, Field(max_length=2_000)] | None = None


class MemoryCandidate(StrictModel):
    type: MemoryType
    content: Annotated[str, Field(min_length=2, max_length=2_000)]
    privacy_level: PrivacyLevel
    sources: list[MemorySourceRef] = Field(default_factory=list, max_length=16)
    importance: float = 0.5
    pin: bool = False
    confidence: float | None = None
    summary: Annotated[str, Field(max_length=2_000)] | None = None
    extractor_version: Annotated[str, Field(max_length=64)] = "rule-v1"
    valid_from: datetime | None = None
    valid_to: datetime | None = None


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    id: int
    user_id: UUID
    type: str
    content: str
    summary: str | None
    privacy_level: str
    importance: float
    pin: bool
    status: str
    confidence: float | None
    valid_from: datetime | None
    valid_to: datetime | None
    superseded_by: int | None
    supersede_reason: str | None
    conflict_with: int | None
    extractor_version: str | None
    created_by: str
    created_at: datetime
    updated_at: datetime
    last_accessed_at: datetime | None
    access_count: int
    embedding_model: str | None
    embedding_dimension: int | None
    embedding_version: str | None


@dataclass(frozen=True, slots=True)
class MemorySourceEntry:
    source_kind: str
    source_id: str
    excerpt_hash: str | None


@dataclass(frozen=True, slots=True)
class SimilarMemory:
    entry: MemoryEntry
    similarity: float


@dataclass(frozen=True, slots=True)
class ConsolidateOutcome:
    decision: ConsolidateDecision
    memory: MemoryEntry
    related: MemoryEntry | None


@dataclass(frozen=True, slots=True)
class DeletionReceipt:
    ledger_id: int
    entity_id: str
    deleted_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DeletionLedgerEntry:
    id: int
    entity_kind: str
    entity_id: str
    deleted_ids: tuple[int, ...]
    requested_by: str
    reason: str | None
    created_at: datetime
