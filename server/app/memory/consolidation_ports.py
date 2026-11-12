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


class ChunkCursor(Protocol):
    """提取块级持久游标（方案 C1/02A）：跨重试记住已落库的顶层块。

    offset 为块在原文中的起点字符位（块边界由 MAX_EXTRACT_INPUT_CHARS 确定，
    文本不变则不变）；record 在该块候选全部落库后调用，record 失败或未调用
    的块在下次重试中完整重跑（存储幂等由同消息防重建兜底）。
    """

    async def done_offsets(self) -> set[int]: ...

    async def record(
        self,
        offset: int,
        *,
        processed: int,
        created: int,
        supported: int,
        conflict: int,
    ) -> None: ...
