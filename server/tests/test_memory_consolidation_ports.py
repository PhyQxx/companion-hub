"""Pure candidate consolidation preserves decisions and repository authority fields."""

import asyncio
from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from test_memory_consolidation_snapshot import Repository

from app.ids import uuid7
from app.memory.consolidation_core import CandidateConsolidator, ConsolidationPolicy
from app.memory.models import MemoryCandidate, MemorySourceRef, SimilarMemory
from app.schemas import PrivacyLevel
from scripts.check_architecture import allowed


def candidate(privacy: PrivacyLevel = PrivacyLevel.L1, **kwargs: Any) -> MemoryCandidate:
    return MemoryCandidate(
        type="semantic",
        content="助手身高为 160 厘米",
        privacy_level=privacy,
        subject_kind="assistant",
        subject_key="assistant:primary",
        sources=[MemorySourceRef(source_kind="message", source_id=str(uuid7()))],
        **kwargs,
    )


@pytest.mark.parametrize(
    "score,decision",
    [
        (None, "created"),
        (0, "created"),
        (0.5999, "created"),
        (0.6, "conflict"),
        (0.8499, "conflict"),
        (0.85, "supported"),
        (1, "supported"),
    ],
)
async def test_consolidation_threshold_boundaries(score: float | None, decision: str) -> None:
    owner = uuid7()
    repository = Repository(owner)
    if score is not None:
        repository.similar = [SimilarMemory(repository.entry, score)]
    result = await CandidateConsolidator(repository).ingest(
        candidate(), user_id=owner, actor="fixture", enforce_sources=True
    )
    assert result.decision == decision
    query = repository.calls[0][1]
    assert query["user_id"] == owner and query["subject_key"] == "assistant:primary"
    assert query["type"] == "semantic" and query["limit"] == 5
    operation = repository.calls[-1][1]
    if decision == "supported":
        assert operation["source_owner_id"] == owner and operation["importance_step"] == 0.05
        assert result.related is repository.entry and result.memory.id == repository.entry.id
    else:
        assert operation["user_id"] == owner and operation["actor"] == "fixture"
        assert operation["enforce_sources"] is True
        assert operation["conflict_with"] == (
            repository.entry.id if decision == "conflict" else None
        )
        assert result.memory.status == ("conflict" if decision == "conflict" else "active")


@pytest.mark.parametrize("same", [False, True])
async def test_exact_slot_precedes_similarity_and_preserves_normalization(same: bool) -> None:
    owner = uuid7()
    repository = Repository(owner)
    repository.slot = [repository.entry]
    repository.similar = [SimilarMemory(repository.entry, 1)]
    value = candidate(fact_key="profile.height")
    if same:
        value = value.model_copy(update={"content": "助手 身高 为 160 厘米。\n"})
    else:
        value = value.model_copy(update={"content": "助手身高为 165 厘米"})
    result = await CandidateConsolidator(repository).ingest(value, user_id=owner)
    assert [name for name, _ in repository.calls] == ["slot", "support" if same else "add"]
    assert result.decision == ("supported" if same else "conflict")
    assert repository.calls[0][1] == {
        "user_id": owner,
        "subject_kind": "assistant",
        "subject_key": "assistant:primary",
        "fact_key": "profile.height",
        "status": "active",
        "limit": 5,
    }
    if same:
        assert repository.calls[-1][1]["source_owner_id"] is None
    else:
        assert result.memory.conflict_with == repository.entry.id


async def test_episodic_events_append_even_when_similar_text_exists() -> None:
    owner = uuid7()
    repository = Repository(owner)
    repository.similar = [SimilarMemory(repository.entry, 1)]
    value = candidate().model_copy(update={"type": "episodic"})
    result = await CandidateConsolidator(repository).ingest(value, user_id=owner)
    assert result.decision == "created" and result.related is None
    assert [name for name, _ in repository.calls] == ["add"]


@pytest.mark.parametrize("privacy", [PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2])
async def test_consolidation_preserves_non_l3_privacy_and_nested_source_ownership(
    privacy: PrivacyLevel,
) -> None:
    owner = uuid7()
    repository = Repository(owner)
    value = candidate(privacy)
    await CandidateConsolidator(repository).ingest(value, user_id=owner, enforce_sources=True)
    written = repository.calls[-1][1]["candidate"]
    assert written.privacy_level == privacy and written is not value
    assert written.sources[0] is not value.sources[0]
    value.sources.clear()
    assert len(written.sources) == 1


@pytest.mark.parametrize("attribute", ["support_threshold", "similar_limit"])
async def test_custom_policy_is_frozen_and_applied_to_similarity_and_support(
    attribute: str,
) -> None:
    policy = ConsolidationPolicy(
        support_threshold=0.9, conflict_threshold=0.7, support_importance_step=0.1, similar_limit=3
    )
    with pytest.raises(FrozenInstanceError):
        setattr(policy, attribute, 10)
    owner = uuid7()
    repository = Repository(owner)
    repository.similar = [SimilarMemory(repository.entry, 0.9)]
    result = await CandidateConsolidator(repository, policy=policy).ingest(
        candidate(), user_id=owner
    )
    assert result.decision == "supported"
    assert repository.calls[0][1]["limit"] == 3
    assert repository.calls[-1][1]["importance_step"] == 0.1


@pytest.mark.parametrize("error", [RuntimeError("fixture"), asyncio.CancelledError()])
async def test_repository_error_propagates_without_attempting_add(error: BaseException) -> None:
    class Broken(Repository):
        async def find_similar(self, *args: Any, **kwargs: Any) -> list[SimilarMemory]:
            raise error

    repository = Broken(uuid7())
    with pytest.raises(type(error)) as failure:
        await CandidateConsolidator(repository).ingest(
            candidate(), user_id=repository.entry.user_id
        )
    assert failure.value is error and repository.calls == []


def test_policy_and_normalizer_legacy_exports_remain_compatible() -> None:
    from app.memory import CandidateConsolidator as exported
    from app.memory.consolidation import ConsolidationPolicy as legacy
    from app.memory.consolidation import _normalize_fact_content

    assert exported is CandidateConsolidator and legacy is ConsolidationPolicy
    assert _normalize_fact_content(" 合成 事实。! ") == "合成事实"


@pytest.mark.parametrize(
    "module", ["app.memory.consolidation_core", "app.memory.consolidation_ports"]
)
@pytest.mark.parametrize(
    "adapter", ["app.db", "app.llm", "app.memory.store", "sqlalchemy", "httpx"]
)
def test_consolidation_core_cannot_import_runtime_adapters(module: str, adapter: str) -> None:
    assert not allowed(module, adapter)
