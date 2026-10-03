"""Candidate consolidation repository; immutable domain views, no SQL or SDK."""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from .models import (
    MemoryCandidate,
    MemoryEntry,
    MemorySourceRef,
    MemoryStatus,
    MemorySubjectKind,
    MemoryType,
    SimilarMemory,
)


class MemoryConsolidationRepository(Protocol):
    async def list_memories(
        self,
        *,
        user_id: UUID,
        subject_kind: MemorySubjectKind,
        subject_key: str,
        fact_key: str,
        status: MemoryStatus,
        limit: int,
    ) -> list[MemoryEntry]: ...

    async def find_similar(
        self,
        content: str,
        *,
        user_id: UUID,
        type: MemoryType,
        subject_kind: MemorySubjectKind,
        subject_key: str,
        limit: int,
    ) -> list[SimilarMemory]: ...

    async def add(
        self,
        candidate: MemoryCandidate,
        *,
        user_id: UUID,
        actor: str = "system",
        status: MemoryStatus = MemoryStatus.ACTIVE,
        conflict_with: int | None = None,
        enforce_sources: bool = False,
    ) -> MemoryEntry: ...

    async def register_support(
        self,
        memory_id: int,
        *,
        sources: Sequence[MemorySourceRef],
        importance_step: float,
        source_owner_id: UUID | None = None,
        user_id: UUID | None = None,
    ) -> MemoryEntry: ...
