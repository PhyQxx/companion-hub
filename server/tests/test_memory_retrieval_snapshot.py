"""Retrieval owns candidate vectors and policy inputs across embedding waits."""

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from test_memory_turn_snapshot import NOW, memory

from app.ids import uuid7
from app.memory.models import MemoryEntry
from app.memory.retrieval import MemoryRetriever, RetrievalPolicy
from app.memory.store import RetrievalCandidate
from app.schemas.common import PrivacyLevel


class Provider:
    def __init__(self) -> None:
        self.model_name, self.version, self.dimension = "fixture", "fixture/1", 2
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.wait = False
        self.calls: list[Sequence[str]] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(tuple(texts))
        self.entered.set()
        if self.wait:
            await self.release.wait()
        return [[1.0, 0.0]]


class Repository:
    def __init__(self, owner: UUID) -> None:
        self.embedding_provider = Provider()
        self.vector_sql_enabled = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        entry = replace(
            memory(),
            user_id=owner,
            subject_kind="user",
            subject_key="user:self",
            fact_key=None,
            content="Unrelated entry",
            embedding_model="fixture",
            embedding_version="fixture/1",
            embedding_dimension=2,
        )
        self.candidates = [RetrievalCandidate(entry=entry, embedding=[1.0, 0.0])]
        self.facts: list[MemoryEntry] = []
        self.recalled: list[tuple[int, float]] | None = None
        self.accessed: list[int] = []

    async def retrieval_candidates(self, user_id: UUID, **kwargs: Any) -> list[RetrievalCandidate]:
        self.calls.append(("candidates", {"user_id": user_id, **kwargs}))
        return self.candidates

    async def fact_candidates(self, **kwargs: Any) -> list[MemoryEntry]:
        self.calls.append(("facts", kwargs))
        return self.facts

    async def vector_recall(
        self, query_vector: Sequence[float], **kwargs: Any
    ) -> list[tuple[int, float]]:
        self.calls.append(("vector", {"query_vector": tuple(query_vector), **kwargs}))
        if self.recalled is not None:
            return self.recalled
        return [(item.entry.id, 1.0) for item in self.candidates]

    async def record_access(self, memory_ids: Sequence[int]) -> None:
        self.accessed.extend(memory_ids)


@pytest.mark.parametrize("change", ["clear", "append", "vector", "quotas", "version", "dimension"])
async def test_retrieval_evidence_and_policy_are_owned_before_embedding_wait(change: str) -> None:
    owner = uuid7()
    repository = Repository(owner)
    repository.embedding_provider.wait = True
    quotas = {"semantic": 1}
    retriever = MemoryRetriever(repository, policy=RetrievalPolicy(quotas=quotas))
    task = asyncio.create_task(
        retriever.retrieve("查找合成证据", user_id=owner, privacy_level=PrivacyLevel.L1, now=NOW)
    )
    try:
        await asyncio.wait_for(repository.embedding_provider.entered.wait(), 3)
        if change == "clear":
            repository.candidates.clear()
        elif change == "append":
            first = repository.candidates[0]
            repository.candidates.append(
                RetrievalCandidate(
                    replace(first.entry, id=2, type="preference"), embedding=[1.0, 0.0]
                )
            )
        elif change == "vector":
            vector = repository.candidates[0].embedding
            assert vector is not None
            vector[:] = [0.0, 1.0]
        elif change == "quotas":
            quotas["semantic"] = 0
        elif change == "version":
            repository.embedding_provider.version = "changed/2"
        else:
            repository.embedding_provider.dimension = 3
        repository.embedding_provider.release.set()
        result = await asyncio.wait_for(task, 3)
        assert [hit.memory.id for hit in result.hits] == [1]
        assert result.candidate_count == 1 and result.vector_recalled == 1
        assert result.hits[0].vector_score == pytest.approx(1.0)
        assert repository.accessed == [1]
    finally:
        repository.embedding_provider.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("route", ["general", "inventory", "exact"])
@pytest.mark.parametrize(
    "changed", ["owner", "privacy", "status", "starts", "expires", "subject_kind", "subject_key"]
)
async def test_retrieval_validates_repository_evidence_before_using_it(
    route: str, changed: str
) -> None:
    owner = uuid7()
    repository = Repository(owner)
    updates: dict[str, dict[str, Any]] = {
        "owner": {"user_id": uuid7()},
        "privacy": {"privacy_level": "L2"},
        "status": {"status": "superseded"},
        "starts": {"valid_from": NOW + timedelta(seconds=1)},
        "expires": {"valid_to": NOW - timedelta(seconds=1)},
        "subject_kind": {"subject_kind": "assistant"},
        "subject_key": {"subject_key": "user:another"},
    }
    entry = replace(repository.candidates[0].entry, fact_key="profile.height", **updates[changed])
    repository.candidates = [RetrievalCandidate(entry, [1.0, 0.0])]
    query = "查找合成证据" if route == "general" else "关于我的记忆有哪些"
    if route == "exact":
        repository.candidates = []
        repository.facts = [entry]
        query = "我的身高"
    result = await MemoryRetriever(repository).retrieve(
        query, user_id=owner, privacy_level=PrivacyLevel.L1, now=NOW
    )
    assert result.hits == () and result.candidate_count == 0
    assert repository.accessed == []


async def test_exact_retrieval_requires_the_requested_fact_slot() -> None:
    owner = uuid7()
    repository = Repository(owner)
    repository.facts = [replace(repository.candidates[0].entry, fact_key="profile.birthday")]
    repository.candidates = []
    result = await MemoryRetriever(repository).retrieve(
        "我的身高", user_id=owner, privacy_level=PrivacyLevel.L1, now=NOW
    )
    assert result.hits == () and result.candidate_count == 0 and repository.accessed == []


@pytest.mark.parametrize(
    "changed", ["unknown", "version", "dimension", "nan", "infinite", "zero", "negative"]
)
async def test_ann_recall_requires_known_vectors_and_finite_positive_scores(changed: str) -> None:
    owner = uuid7()
    repository = Repository(owner)
    repository.vector_sql_enabled = True
    entry = repository.candidates[0].entry
    if changed == "version":
        repository.candidates[0] = RetrievalCandidate(
            replace(entry, embedding_version="other/2"), [1.0, 0.0]
        )
    elif changed == "dimension":
        repository.candidates[0] = RetrievalCandidate(
            replace(entry, embedding_dimension=3), [1.0, 0.0]
        )
    scores = {"nan": float("nan"), "infinite": float("inf"), "zero": 0.0, "negative": -0.5}
    repository.recalled = [(999 if changed == "unknown" else entry.id, scores.get(changed, 1.0))]
    result = await MemoryRetriever(repository).retrieve(
        "查找合成证据", user_id=owner, privacy_level=PrivacyLevel.L1, now=NOW
    )
    assert result.hits == () and result.vector_recalled == 0 and result.candidate_count == 1
    assert repository.accessed == []
