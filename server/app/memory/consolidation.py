# ruff: noqa: RUF001
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import final
from uuid import UUID

from app.schemas.common import PrivacyLevel

from .models import (
    ConsolidateDecision,
    ConsolidateOutcome,
    MemoryCandidate,
    MemorySourceKind,
    MemorySourceRef,
    MemoryStatus,
    MemoryType,
)
from .store import MemoryStore

RULE_EXTRACTOR_VERSION = "rule-v1"

_SENTENCE_SPLIT = re.compile(r"[。！？!?\n；;]+")
_TIME_WORDS = re.compile(
    r"(明天|后天|今天|下周|本周|下个月|月底|周[一二三四五六日天]|\d{1,2}月\d{1,2}[号日]|\d{1,2}号)"
)
_COMMITMENT_MODAL = re.compile(r"(要|得|会|准备|打算|想去|要去|得去|将)")
_PREFERENCE_PATTERNS = (
    re.compile(r"我(不吃|不喝|不爱吃|不喜欢吃|讨厌吃|忌口?)"),
    re.compile(r"我(最喜欢?|爱|偏好|喜欢)"),
    re.compile(r"我(不喜欢|不爱|讨厌|不想)"),
)
_SEMANTIC_PATTERN = re.compile(r"我(是|姓|叫|住在|家在|从事|工作是|在.{1,12}(工作|上学|生活))")
_PERSONA_DIRECTED = re.compile(r"[你妳]")


@dataclass(frozen=True, slots=True)
class ConsolidationPolicy:
    support_threshold: float = 0.85
    conflict_threshold: float = 0.60
    support_importance_step: float = 0.05
    similar_limit: int = 5


@final
class MemoryIngester:
    """Adjudicates incoming candidates against same-type active memories.

    Model- or rule-derived candidates never overwrite a stable fact directly:
    a related-but-different statement parks as a conflicted memory and waits
    for an explicit resolution, matching the memory design in docs/02 §3.6.
    """

    def __init__(self, store: MemoryStore, *, policy: ConsolidationPolicy | None = None) -> None:
        self._store = store
        self._policy = policy or ConsolidationPolicy()

    async def ingest(
        self, candidate: MemoryCandidate, *, user_id: UUID, actor: str = "extractor"
    ) -> ConsolidateOutcome:
        similar = await self._store.find_similar(
            candidate.content,
            user_id=user_id,
            type=MemoryType(candidate.type),
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
    ) -> list[ConsolidateOutcome]:
        # L2/L3 conversations must not auto-persist content: the desensitizing
        # utility extractor is a later milestone, so v1 skips them entirely.
        if privacy_level in {PrivacyLevel.L2, PrivacyLevel.L3}:
            return []
        candidates = RuleBasedExtractor().extract(
            text, message_id=message_id, occurred_at=occurred_at
        )
        return [
            await self.ingest(candidate, user_id=user_id, actor=RULE_EXTRACTOR_VERSION)
            for candidate in candidates
        ]


@final
class RuleBasedExtractor:
    """Conservative deterministic extractor for first-person stable statements."""

    def extract(
        self, text: str, *, message_id: UUID, occurred_at: datetime
    ) -> list[MemoryCandidate]:
        results: list[MemoryCandidate] = []
        seen: set[str] = set()
        for raw_sentence in _SENTENCE_SPLIT.split(text):
            sentence = raw_sentence.strip()
            if not (2 <= len(sentence) <= 200):
                continue
            first_person = sentence.find("我")
            if first_person < 0:
                continue
            clause = sentence[first_person:]
            memory_type = _classify(clause)
            if memory_type is None or clause in seen:
                continue
            seen.add(clause)
            results.append(
                MemoryCandidate(
                    type=memory_type,
                    content=_third_person(clause),
                    privacy_level=PrivacyLevel.L1,
                    sources=[
                        MemorySourceRef(
                            source_kind=MemorySourceKind.MESSAGE,
                            source_id=str(message_id),
                            excerpt=clause,
                        )
                    ],
                    importance=_default_importance(memory_type),
                    confidence=0.6,
                    extractor_version=RULE_EXTRACTOR_VERSION,
                    valid_from=occurred_at,
                    valid_to=(
                        occurred_at + timedelta(days=7)
                        if memory_type is MemoryType.COMMITMENT
                        else None
                    ),
                )
            )
        return results[:4]


def _classify(sentence: str) -> MemoryType | None:
    if not sentence.startswith("我"):
        return None
    if _TIME_WORDS.search(sentence) and _COMMITMENT_MODAL.search(sentence):
        return MemoryType.COMMITMENT
    if any(pattern.search(sentence) for pattern in _PREFERENCE_PATTERNS):
        return None if _PERSONA_DIRECTED.search(sentence) else MemoryType.PREFERENCE
    if _SEMANTIC_PATTERN.search(sentence):
        return None if _PERSONA_DIRECTED.search(sentence) else MemoryType.SEMANTIC
    return None


def _third_person(sentence: str) -> str:
    if sentence.startswith("我"):
        return f"用户{sentence[1:]}"
    return sentence


def _default_importance(memory_type: MemoryType) -> float:
    if memory_type is MemoryType.COMMITMENT:
        return 0.7
    if memory_type in {MemoryType.PREFERENCE, MemoryType.SEMANTIC}:
        return 0.6
    return 0.5
