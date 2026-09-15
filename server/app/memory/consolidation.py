from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import final
from uuid import UUID

from app.schemas.common import PrivacyLevel

from .extraction import (
    RULE_EXTRACTOR_VERSION,
    ExtractionBackend,
    MemoryExtractor,
    RuleBasedExtractor,
    TurnMemoryExtractor,
)
from .models import (
    ConsolidateDecision,
    ConsolidateOutcome,
    MemoryCandidate,
    MemoryEntry,
    MemoryStatus,
    MemorySubjectKind,
    MemoryType,
)
from .store import MemoryStore

__all__ = [
    "ConsolidateDecision",
    "ConsolidateOutcome",
    "ConsolidationPolicy",
    "MemoryIngester",
]


@dataclass(frozen=True, slots=True)
class ConsolidationPolicy:
    support_threshold: float = 0.85
    conflict_threshold: float = 0.60
    support_importance_step: float = 0.05
    similar_limit: int = 5


@final
class MemoryIngester:
    """对候选记忆做同类型相似度判定后入库。

    模型或规则产出的候选永远不会直接覆盖稳定事实：相似但不同的表述
    会以 conflict 状态挂起，等待显式裁决——这正是 docs/00 §3.6 的
    "冲突时保留版本并等待确认"语义。
    """

    def __init__(
        self,
        store: MemoryStore,
        *,
        policy: ConsolidationPolicy | None = None,
        extractor: MemoryExtractor | None = None,
    ) -> None:
        self._store = store
        self._policy = policy or ConsolidationPolicy()
        self._extractor: MemoryExtractor = extractor or RuleBasedExtractor()
        self._turn_extractor = TurnMemoryExtractor(self._extractor)

    async def ingest(
        self, candidate: MemoryCandidate, *, user_id: UUID, actor: str = "extractor"
    ) -> ConsolidateOutcome:
        if candidate.fact_key is not None:
            slot = await self._store.list_memories(
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
                    updated = await self._store.register_support(
                        current.id,
                        sources=candidate.sources,
                        importance_step=self._policy.support_importance_step,
                    )
                    return ConsolidateOutcome(
                        decision=ConsolidateDecision.SUPPORTED,
                        memory=updated,
                        related=current,
                    )
                created = await self._store.add(
                    candidate,
                    user_id=user_id,
                    actor=actor,
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
            created = await self._store.add(candidate, user_id=user_id, actor=actor)
            return ConsolidateOutcome(
                decision=ConsolidateDecision.CREATED, memory=created, related=None
            )

        similar = await self._store.find_similar(
            candidate.content,
            user_id=user_id,
            type=MemoryType(candidate.type),
            subject_kind=candidate.subject_kind,
            subject_key=candidate.subject_key,
            limit=self._policy.similar_limit,
        )
        best = similar[0] if similar else None
        if best is not None and best.similarity >= self._policy.support_threshold:
            updated = await self._store.register_support(
                best.entry.id,
                sources=candidate.sources,
                importance_step=self._policy.support_importance_step,
            )
            return ConsolidateOutcome(
                decision=ConsolidateDecision.SUPPORTED, memory=updated, related=best.entry
            )
        if best is not None and best.similarity >= self._policy.conflict_threshold:
            created = await self._store.add(
                candidate,
                user_id=user_id,
                actor=actor,
                status=MemoryStatus.CONFLICT,
                conflict_with=best.entry.id,
            )
            return ConsolidateOutcome(
                decision=ConsolidateDecision.CONFLICT, memory=created, related=best.entry
            )
        created = await self._store.add(candidate, user_id=user_id, actor=actor)
        return ConsolidateOutcome(
            decision=ConsolidateDecision.CREATED, memory=created, related=None
        )

    async def ingest_message(
        self,
        *,
        user_id: UUID,
        message_id: UUID,
        text: str,
        privacy_level: PrivacyLevel,
        occurred_at: datetime,
        backend: ExtractionBackend | None = None,
    ) -> list[ConsolidateOutcome]:
        # 归一化隐私等级（上游传来的可能是 pydantic 展开后的字符串）
        privacy = PrivacyLevel(privacy_level)
        candidates = await self._extractor.extract(
            text,
            message_id=message_id,
            privacy_level=privacy,
            occurred_at=occurred_at,
            backend=backend,
        )
        return [
            await self.ingest(
                candidate,
                user_id=user_id,
                actor=candidate.extractor_version or RULE_EXTRACTOR_VERSION,
            )
            for candidate in candidates
        ]

    async def ingest_turn(
        self,
        *,
        user_id: UUID,
        user_message_id: UUID,
        user_text: str,
        user_occurred_at: datetime,
        assistant_message_id: UUID,
        assistant_text: str,
        assistant_occurred_at: datetime,
        privacy_level: PrivacyLevel,
        retrieved_memories: tuple[MemoryEntry, ...] = (),
        backend: ExtractionBackend | None = None,
    ) -> list[ConsolidateOutcome]:
        candidates = await self._turn_extractor.extract_turn(
            user_text=user_text,
            user_message_id=user_message_id,
            user_occurred_at=user_occurred_at,
            assistant_text=assistant_text,
            assistant_message_id=assistant_message_id,
            assistant_occurred_at=assistant_occurred_at,
            privacy_level=privacy_level,
            retrieved_memories=retrieved_memories,
            backend=backend,
        )
        return [
            await self.ingest(
                candidate,
                user_id=user_id,
                actor=candidate.extractor_version or RULE_EXTRACTOR_VERSION,
            )
            for candidate in candidates
        ]


def _normalize_fact_content(content: str) -> str:
    """槽位事实使用保守文本归一化；不同值宁可进入 conflict，也不静默覆盖。"""

    return "".join(content.split()).rstrip("。.!")
