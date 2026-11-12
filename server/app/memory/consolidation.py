from __future__ import annotations

import logging
from datetime import datetime
from typing import final
from uuid import UUID

from app.schemas.common import PrivacyLevel

from .consolidation_core import CandidateConsolidator
from .consolidation_core import ConsolidationPolicy as ConsolidationPolicy
from .consolidation_core import _normalize_fact_content as _normalize_fact_content
from .consolidation_ports import ChunkCursor, MemoryConsolidationRepository
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
)

__all__ = [
    "ConsolidateDecision",
    "ConsolidateOutcome",
    "ConsolidationPolicy",
    "MemoryIngester",
]


@final
class MemoryIngester:
    """对候选记忆做同类型相似度判定后入库。

    模型或规则产出的候选永远不会直接覆盖稳定事实：相似但不同的表述
    会以 conflict 状态挂起，等待显式裁决——这正是 docs/00 §3.6 的
    "冲突时保留版本并等待确认"语义。
    """

    def __init__(
        self,
        store: MemoryConsolidationRepository,
        *,
        policy: ConsolidationPolicy | None = None,
        extractor: MemoryExtractor | None = None,
    ) -> None:
        self._store = store
        self._policy = policy or ConsolidationPolicy()
        self._consolidator = CandidateConsolidator(store, policy=self._policy)
        self._extractor: MemoryExtractor = extractor or RuleBasedExtractor()
        self._turn_extractor = TurnMemoryExtractor(self._extractor)

    async def ingest(
        self,
        candidate: MemoryCandidate,
        *,
        user_id: UUID,
        actor: str = "extractor",
        enforce_sources: bool = False,
    ) -> ConsolidateOutcome:
        sources = [source.source_id for source in candidate.sources]
        try:
            outcome = await self._consolidator.ingest(
                candidate, user_id=user_id, actor=actor, enforce_sources=enforce_sources
            )
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "memory.persist sources=%s reason=write_failed error_type=%s",
                sources,
                type(exc).__name__,
            )
            raise
        logging.getLogger(__name__).info(
            "memory.persist sources=%s decision=%s memory_id=%s related_id=%s",
            sources,
            outcome.decision,
            outcome.memory.id,
            outcome.related.id if outcome.related else None,
        )
        return outcome

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
        enforce_sources: bool = False,
        done_offsets: frozenset[int] = frozenset(),
        chunk_cursor: ChunkCursor | None = None,
    ) -> list[ConsolidateOutcome]:
        """沉淀完整回合；可选块级游标（方案 C1/02A）支持断点续跑。

        chunk_cursor 提供已完成块集合与记录回调：每个顶层块提取完成后立即
        落库并记录游标，后续重试跳过该块（候选已持久，模型不再重跑）；
        turn_core 会深拷贝候选，已处理判断按 (type, content) 键而非身份。
        """

        async def on_chunk(offset: int, chunk: list[MemoryCandidate]) -> None:
            outcomes = [
                await self.ingest(
                    candidate,
                    user_id=user_id,
                    actor=candidate.extractor_version or RULE_EXTRACTOR_VERSION,
                    enforce_sources=enforce_sources,
                )
                for candidate in chunk
            ]
            chunked_outcomes.extend(outcomes)
            chunked_keys.update((candidate.type, candidate.content) for candidate in chunk)
            if chunk_cursor is not None:
                counts = {"created": 0, "supported": 0, "conflict": 0}
                for outcome in outcomes:
                    key = str(outcome.decision)
                    if key in counts:
                        counts[key] += 1
                await chunk_cursor.record(offset, processed=len(chunk), **counts)

        chunked_outcomes: list[ConsolidateOutcome] = []
        chunked_keys: set[tuple[str, str]] = set()
        skip = frozenset(await chunk_cursor.done_offsets()) if chunk_cursor else done_offsets
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
            skip_offsets=skip,
            chunk_done=on_chunk if chunk_cursor is not None else None,
        )
        logging.getLogger(__name__).info(
            "memory.turn message_id=%s reason=%s candidates=%d chunked=%d skipped=%d",
            user_message_id,
            "candidates_ready" if candidates else "no_candidates",
            len(candidates),
            len(chunked_outcomes),
            len(skip),
        )
        outcomes = list(chunked_outcomes)
        outcomes.extend(
            [
                await self.ingest(
                    candidate,
                    user_id=user_id,
                    actor=candidate.extractor_version or RULE_EXTRACTOR_VERSION,
                    enforce_sources=enforce_sources,
                )
                for candidate in candidates
                if (candidate.type, candidate.content) not in chunked_keys
            ]
        )
        return outcomes
