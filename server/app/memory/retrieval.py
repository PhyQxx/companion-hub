# ruff: noqa: RUF001
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import final
from uuid import UUID

from app.schemas.common import PrivacyLevel

from .embeddings import cosine_similarity, lexical_cosine, text_tokens
from .models import MemoryEntry, MemoryStatus, MemoryType
from .store import MemoryStore, RetrievalCandidate

RETRIEVAL_POLICY_VERSION = "hybrid-quota-v1"

DEFAULT_TYPE_QUOTAS: Mapping[str, int] = {
    MemoryType.SEMANTIC.value: 3,
    MemoryType.PREFERENCE.value: 2,
    MemoryType.COMMITMENT.value: 1,
    MemoryType.EPISODIC.value: 2,
    MemoryType.EMOTIONAL.value: 2,
}


@dataclass(frozen=True, slots=True)
class MemoryHit:
    memory: MemoryEntry
    vector_score: float
    lexical_score: float
    final_score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    hits: tuple[MemoryHit, ...]
    policy_version: str
    candidate_count: int
    vector_recalled: int
    lexical_recalled: int


@dataclass(frozen=True, slots=True)
class RetrievalPolicy:
    top_k: int = 8
    recall_k: int = 30
    candidate_limit: int = 500
    quotas: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_TYPE_QUOTAS))
    relevance_weight: float = 0.55
    importance_weight: float = 0.25
    pin_bonus: float = 0.2
    episodic_recency_weight: float = 0.15
    recency_tau_days: float = 30.0


@final
class MemoryRetriever:
    def __init__(self, store: MemoryStore, *, policy: RetrievalPolicy | None = None) -> None:
        self._store = store
        self._policy = policy or RetrievalPolicy()

    async def retrieve(
        self,
        query: str,
        *,
        user_id: UUID,
        privacy_level: PrivacyLevel,
        now: datetime | None = None,
    ) -> RetrievalResult:
        moment = now or datetime.now(UTC)
        # L2 memories may only join contexts that stay on the private route.
        allowed_levels = (
            (PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2)
            if privacy_level is PrivacyLevel.L2
            else (PrivacyLevel.L0, PrivacyLevel.L1)
        )
        candidates = await self._store.retrieval_candidates(
            user_id,
            statuses=(MemoryStatus.ACTIVE,),
            privacy_levels=allowed_levels,
            valid_at=moment,
            limit=self._policy.candidate_limit,
        )
        provider = self._store.embedding_provider
        query_vector = (await provider.embed([query]))[0]
        query_tokens = text_tokens(query)

        def similarity_of(item: RetrievalCandidate) -> float:
            if (
                item.embedding is None
                or item.entry.embedding_version != provider.version
                or len(item.embedding) != provider.dimension
            ):
                return 0.0
            return cosine_similarity(query_vector, item.embedding)

        vector_ranked = _rank(candidates, similarity_of)
        lexical_ranked = _rank(
            candidates,
            lambda item: lexical_cosine(query_tokens, text_tokens(item.entry.content)),
        )
        vector_top = dict(vector_ranked[: self._policy.recall_k])
        lexical_top = dict(lexical_ranked[: self._policy.recall_k])

        by_id = {item.entry.id: item for item in candidates}
        ranked: list[MemoryHit] = []
        for memory_id in set(vector_top) | set(lexical_top):
            entry = by_id[memory_id].entry
            vector_score = min(vector_top.get(memory_id, 0.0), 1.0)
            lexical_score = min(lexical_top.get(memory_id, 0.0), 1.0)
            relevance = max(vector_score, lexical_score)
            score = (
                self._policy.relevance_weight * relevance
                + self._policy.importance_weight * entry.importance
            )
            reasons: list[str] = []
            if vector_score > 0:
                reasons.append("vector")
            if lexical_score > 0:
                reasons.append("lexical")
            if entry.pin:
                score += self._policy.pin_bonus
                reasons.append("pin")
            if entry.type == MemoryType.EPISODIC.value and entry.created_at is not None:
                age_days = max((moment - entry.created_at).total_seconds() / 86_400.0, 0.0)
                score += self._policy.episodic_recency_weight * math.exp(
                    -age_days / self._policy.recency_tau_days
                )
                reasons.append("recency")
            ranked.append(
                MemoryHit(
                    memory=entry,
                    vector_score=vector_score,
                    lexical_score=lexical_score,
                    final_score=score,
                    reasons=tuple(reasons),
                )
            )
        ranked.sort(key=lambda hit: hit.final_score, reverse=True)

        selected: list[MemoryHit] = []
        used: Counter[str] = Counter()
        for hit in ranked:
            quota = self._policy.quotas.get(hit.memory.type)
            if quota is not None and used[hit.memory.type] >= quota:
                continue
            selected.append(hit)
            used[hit.memory.type] += 1
            if len(selected) >= self._policy.top_k:
                break

        await self._store.record_access([hit.memory.id for hit in selected])
        return RetrievalResult(
            hits=tuple(selected),
            policy_version=RETRIEVAL_POLICY_VERSION,
            candidate_count=len(candidates),
            vector_recalled=len(vector_top),
            lexical_recalled=len(lexical_top),
        )

    @staticmethod
    def render_context(result: RetrievalResult) -> str:
        if not result.hits:
            return ""
        lines = [
            f"- [{hit.memory.type}] {hit.memory.content}"
            + (f"（{hit.memory.summary}）" if hit.memory.summary else "")
            for hit in result.hits
        ]
        return (
            "【相关记忆】以下是关于用户的长期记忆，仅供自然参考："
            "只依据记忆谈论用户，不确定就问；不要逐条罗列或声称拥有记忆列表。\n"
            + "\n".join(lines)
        )


def _rank(
    candidates: Sequence[RetrievalCandidate],
    key: Callable[[RetrievalCandidate], float],
) -> list[tuple[int, float]]:
    ranked = [(item.entry.id, key(item)) for item in candidates]
    ranked = [(memory_id, score) for memory_id, score in ranked if score > 0]
    ranked.sort(key=lambda pair: pair[1], reverse=True)
    return ranked
