"""Candidate consolidation must not persist L3 support or lose source snapshots."""

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from test_memory_turn_snapshot import memory

from app.ids import uuid7
from app.memory.consolidation import MemoryIngester
from app.memory.models import (
    MemoryCandidate,
    MemoryEntry,
    MemorySourceRef,
    MemoryStatus,
    MemorySubjectKind,
    MemoryType,
    SimilarMemory,
)


class Repository:
    def __init__(self, owner: UUID) -> None:
        self.entry = replace(memory(), user_id=owner)
        self.slot: list[MemoryEntry] = []
        self.similar: list[SimilarMemory] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.wait = False

    async def list_memories(
        self,
        *,
        user_id: UUID,
        subject_kind: MemorySubjectKind,
        subject_key: str,
        fact_key: str,
        status: MemoryStatus,
        limit: int,
    ) -> list[MemoryEntry]:
        self.calls.append(
            (
                "slot",
                {
                    "user_id": user_id,
                    "subject_kind": subject_kind,
                    "subject_key": subject_key,
                    "fact_key": fact_key,
                    "status": status,
                    "limit": limit,
                },
            )
        )
        self.entered.set()
        if self.wait:
            await self.release.wait()
        return self.slot

    async def find_similar(
        self,
        content: str,
        *,
        user_id: UUID,
        type: MemoryType,
        subject_kind: MemorySubjectKind,
        subject_key: str,
        limit: int,
    ) -> list[SimilarMemory]:
        self.calls.append(
            (
                "similar",
                {
                    "content": content,
                    "user_id": user_id,
                    "type": type,
                    "subject_kind": subject_kind,
                    "subject_key": subject_key,
                    "limit": limit,
                },
            )
        )
        self.entered.set()
        if self.wait:
            await self.release.wait()
        return self.similar

    async def add(
        self,
        candidate: MemoryCandidate,
        *,
        user_id: UUID,
        actor: str = "system",
        status: MemoryStatus = MemoryStatus.ACTIVE,
        conflict_with: int | None = None,
        enforce_sources: bool = False,
    ) -> MemoryEntry:
        self.calls.append(
            (
                "add",
                {
                    "candidate": candidate,
                    "user_id": user_id,
                    "actor": actor,
                    "status": status,
                    "conflict_with": conflict_with,
                    "enforce_sources": enforce_sources,
                },
            )
        )
        if candidate.privacy_level == "L3":
            raise ValueError("L3 content must never become a durable memory")
        return replace(
            self.entry,
            id=2,
            content=candidate.content,
            status=str(status),
            conflict_with=conflict_with,
        )

    async def register_support(
        self,
        memory_id: int,
        *,
        sources: Sequence[MemorySourceRef],
        importance_step: float,
        source_owner_id: UUID | None = None,
    ) -> MemoryEntry:
        self.calls.append(
            (
                "support",
                {
                    "memory_id": memory_id,
                    "sources": tuple(sources),
                    "importance_step": importance_step,
                    "source_owner_id": source_owner_id,
                },
            )
        )
        return self.entry


@pytest.mark.parametrize("slot", [False, True])
async def test_l3_candidate_never_supports_existing_memory(slot: bool) -> None:
    owner = uuid7()
    repository = Repository(owner)
    repository.slot = [repository.entry] if slot else []
    repository.similar = [SimilarMemory(repository.entry, 1)]
    candidate = MemoryCandidate(
        type="semantic",
        content=repository.entry.content,
        privacy_level="L3",
        subject_kind="assistant",
        subject_key="assistant:primary",
        fact_key="profile.height" if slot else None,
        sources=[MemorySourceRef(source_kind="event", source_id="synthetic-l3")],
    )
    with pytest.raises(ValueError, match="L3"):
        await MemoryIngester(repository).ingest(candidate, user_id=owner)
    assert repository.calls == []


async def test_consolidation_keeps_source_list_from_before_repository_wait() -> None:
    owner, source = uuid7(), uuid7()
    repository = Repository(owner)
    repository.wait = True
    repository.similar = [SimilarMemory(repository.entry, 1)]
    candidate = MemoryCandidate(
        type="semantic",
        content=repository.entry.content,
        privacy_level="L1",
        subject_kind="assistant",
        subject_key="assistant:primary",
        sources=[MemorySourceRef(source_kind="message", source_id=str(source))],
    )
    task = asyncio.create_task(
        MemoryIngester(repository).ingest(candidate, user_id=owner, enforce_sources=True)
    )
    try:
        await asyncio.wait_for(repository.entered.wait(), 3)
        candidate.sources.clear()
        repository.release.set()
        result = await asyncio.wait_for(task, 3)
        assert result.decision == "supported"
        support = repository.calls[-1][1]
        assert support["source_owner_id"] == owner
        assert [item.source_id for item in support["sources"]] == [str(source)]
    finally:
        repository.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
