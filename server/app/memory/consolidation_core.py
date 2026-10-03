"""Candidate consolidation policy detached from model requests and SQL storage."""

from dataclasses import dataclass
from uuid import UUID

from app.schemas.common import PrivacyLevel

from .consolidation_ports import MemoryConsolidationRepository
from .models import (
    ConsolidateDecision,
    ConsolidateOutcome,
    MemoryCandidate,
    MemoryStatus,
    MemorySubjectKind,
    MemoryType,
)


@dataclass(frozen=True, slots=True)
class ConsolidationPolicy:
    support_threshold: float = 0.85
    conflict_threshold: float = 0.60
    support_importance_step: float = 0.05
    similar_limit: int = 5


class CandidateConsolidator:
    def __init__(
        self,
        repository: MemoryConsolidationRepository,
        *,
        policy: ConsolidationPolicy | None = None,
    ) -> None:
        self._repository = repository
        self._policy = policy or ConsolidationPolicy()

    async def ingest(
        self,
        candidate: MemoryCandidate,
        *,
        user_id: UUID,
        actor: str = "extractor",
        enforce_sources: bool = False,
    ) -> ConsolidateOutcome:
        if PrivacyLevel(candidate.privacy_level) is PrivacyLevel.L3:
            raise ValueError("L3 content must never become a durable memory")
        # Own nested source lists before the repository can await.
        candidate = candidate.model_copy(deep=True)

        if candidate.fact_key is not None:
            slot = await self._repository.list_memories(
                user_id=user_id,
                subject_kind=MemorySubjectKind(candidate.subject_kind),
                subject_key=candidate.subject_key,
                fact_key=candidate.fact_key,
                status=MemoryStatus.ACTIVE,
                limit=5,
            )
            if slot:
                current = slot[0]
                if _normalize_fact_content(current.content) == _normalize_fact_content(
                    candidate.content
                ):
                    updated = await self._repository.register_support(
                        current.id,
                        sources=candidate.sources,
                        source_owner_id=user_id if enforce_sources else None,
                        importance_step=self._policy.support_importance_step,
                    )
                    return ConsolidateOutcome(
                        decision=ConsolidateDecision.SUPPORTED,
                        memory=updated,
                        related=current,
                    )
                created = await self._repository.add(
                    candidate,
                    user_id=user_id,
                    actor=actor,
                    enforce_sources=enforce_sources,
                    status=MemoryStatus.CONFLICT,
                    conflict_with=current.id,
                )
                return ConsolidateOutcome(
                    decision=ConsolidateDecision.CONFLICT,
                    memory=created,
                    related=current,
                )

        # 情景记忆描述的是在不同时间发生的事件。即使两次事件的文本很相似，
        # 它们也可以同时为真，不能套用稳定事实的“相似但不同即冲突”规则。
        # 上游事件管线负责按事件 ID、截图哈希和时间窗口去重；这里按事件追加。
        if MemoryType(candidate.type) is MemoryType.EPISODIC:
            created = await self._repository.add(
                candidate, user_id=user_id, actor=actor, enforce_sources=enforce_sources
            )
            return ConsolidateOutcome(
                decision=ConsolidateDecision.CREATED, memory=created, related=None
            )

        similar = await self._repository.find_similar(
            candidate.content,
            user_id=user_id,
            type=MemoryType(candidate.type),
            subject_kind=candidate.subject_kind,
            subject_key=candidate.subject_key,
            limit=self._policy.similar_limit,
        )
        best = similar[0] if similar else None
        if best is not None and best.similarity >= self._policy.support_threshold:
            updated = await self._repository.register_support(
                best.entry.id,
                sources=candidate.sources,
                source_owner_id=user_id if enforce_sources else None,
                importance_step=self._policy.support_importance_step,
            )
            return ConsolidateOutcome(
                decision=ConsolidateDecision.SUPPORTED, memory=updated, related=best.entry
            )
        if best is not None and best.similarity >= self._policy.conflict_threshold:
            created = await self._repository.add(
                candidate,
                user_id=user_id,
                actor=actor,
                enforce_sources=enforce_sources,
                status=MemoryStatus.CONFLICT,
                conflict_with=best.entry.id,
            )
            return ConsolidateOutcome(
                decision=ConsolidateDecision.CONFLICT, memory=created, related=best.entry
            )
        created = await self._repository.add(
            candidate, user_id=user_id, actor=actor, enforce_sources=enforce_sources
        )
        return ConsolidateOutcome(
            decision=ConsolidateDecision.CREATED, memory=created, related=None
        )


def _normalize_fact_content(content: str) -> str:
    """槽位事实使用保守文本归一化；不同值宁可进入 conflict，也不静默覆盖。"""

    return "".join(content.split()).rstrip("。.!")
