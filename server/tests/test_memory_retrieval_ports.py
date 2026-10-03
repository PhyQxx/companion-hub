"""Detached retrieval contracts preserve privacy, routing and compatibility."""

import asyncio
from collections.abc import MutableMapping
from dataclasses import replace
from typing import Any, cast

import pytest
from test_memory_retrieval_snapshot import Repository
from test_memory_turn_snapshot import NOW

from app.ids import uuid7
from app.memory.retrieval import MemoryRetriever, RetrievalPolicy
from app.memory.retrieval_models import RetrievalCandidate
from app.schemas.common import PrivacyLevel
from scripts.check_architecture import allowed


@pytest.mark.parametrize(
    "privacy,expected",
    [(PrivacyLevel.L0, 0), (PrivacyLevel.L1, 1), (PrivacyLevel.L2, 1), (PrivacyLevel.L3, 0)],
)
async def test_retrieval_privacy_and_no_private_adapter_calls(
    privacy: PrivacyLevel, expected: int
) -> None:
    owner = uuid7()
    repository = Repository(owner)
    result = await MemoryRetriever(repository).retrieve(
        "查找合成证据", user_id=owner, privacy_level=privacy, now=NOW
    )
    assert len(result.hits) == expected
    if privacy is PrivacyLevel.L3:
        assert repository.calls == [] and repository.embedding_provider.calls == []
    else:
        query = repository.calls[0][1]
        assert query["user_id"] == owner and query["valid_at"] == NOW and query["limit"] == 500
        assert query["statuses"] == ("active",)
        assert repository.accessed == ([1] if expected else [])


@pytest.mark.parametrize(
    "subject,query",
    [
        ("user", "关于我的记忆有哪些"),
        ("assistant", "关于你自己的记忆有哪些"),
        ("shared", "我们都记得哪些"),
    ],
)
async def test_subject_inventory_skips_embedding_and_preserves_subjects(
    subject: str, query: str
) -> None:
    owner = uuid7()
    repository = Repository(owner)
    original = repository.candidates[0].entry
    keys = {
        "user": "user:self",
        "assistant": "assistant:primary",
        "shared": "shared:user-assistant",
    }
    repository.candidates = [
        RetrievalCandidate(
            replace(original, id=index, subject_kind=kind, subject_key=key), [1.0, 0.0]
        )
        for index, (kind, key) in enumerate(keys.items(), 1)
    ]
    result = await MemoryRetriever(repository).retrieve(
        query, user_id=owner, privacy_level=PrivacyLevel.L1, now=NOW
    )
    assert len(result.hits) == 1 and result.hits[0].memory.subject_kind == subject
    assert result.hits[0].reasons == ("subject_inventory",) and result.candidate_count == 3
    assert repository.embedding_provider.calls == []
    assert len(repository.calls) == 1
    assert MemoryRetriever.render_context(result)


async def test_ann_query_passes_owned_filters_and_captured_vector_space() -> None:
    owner = uuid7()
    repository = Repository(owner)
    repository.vector_sql_enabled = True
    result = await MemoryRetriever(repository).retrieve(
        "查找合成证据", user_id=owner, privacy_level=PrivacyLevel.L1, now=NOW
    )
    query = repository.calls[1][1]
    assert query["user_id"] == owner and query["embedding_version"] == "fixture/1"
    assert query["privacy_levels"] == ["L0", "L1"] and query["valid_at"] == NOW
    assert query["subject_keys"] == ["user:self", "assistant:primary", "shared:user-assistant"]
    assert query["query_vector"] == (1.0, 0.0) and query["limit"] == 30
    assert len(result.hits) == 1 and result.vector_recalled == 1


@pytest.mark.parametrize("stage", ["candidates", "embedding", "facts", "access"])
@pytest.mark.parametrize("error", [RuntimeError("fixture"), asyncio.CancelledError("fixture")])
async def test_retrieval_port_errors_and_cancellation_are_not_retried(
    stage: str,
    error: BaseException,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = uuid7()
    repository = Repository(owner)
    calls = 0

    async def rejected(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise error

    targets = {
        "candidates": "retrieval_candidates",
        "facts": "fact_candidates",
        "access": "record_access",
    }
    if stage == "embedding":
        monkeypatch.setattr(repository.embedding_provider, "embed", rejected)
    else:
        monkeypatch.setattr(repository, targets[stage], rejected)
    with pytest.raises(type(error)) as caught:
        await MemoryRetriever(repository).retrieve(
            "我的身高", user_id=owner, privacy_level=PrivacyLevel.L1, now=NOW
        )
    assert caught.value is error and calls == 1


def test_retrieval_quotas_and_legacy_helpers_preserve_identity() -> None:
    from app.memory import embeddings, retrieval, retrieval_models, similarity, store

    policy = RetrievalPolicy()
    with pytest.raises(TypeError):
        cast(MutableMapping[str, int], policy.quotas)["semantic"] = 0
    assert store.RetrievalCandidate is retrieval_models.RetrievalCandidate
    for name in ["_ngrams", "cosine_similarity", "lexical_cosine", "text_tokens"]:
        assert getattr(embeddings, name) is getattr(similarity, name)
    assert retrieval.RetrievalResult is retrieval_models.RetrievalResult


@pytest.mark.parametrize(
    "module", ["app.memory.retrieval", "app.memory.retrieval_ports", "app.memory.similarity"]
)
@pytest.mark.parametrize(
    "adapter",
    ["sqlalchemy", "httpx", "app.memory.store", "app.memory.embeddings", "app.llm.provider"],
)
def test_retrieval_policy_rejects_concrete_adapters(module: str, adapter: str) -> None:
    assert not allowed(module, adapter)
