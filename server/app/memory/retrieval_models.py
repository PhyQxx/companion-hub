"""Detached retrieval results, independent of stores and query strategies."""

from dataclasses import dataclass

from .models import MemoryEntry


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
    subject_hint: str | None = None
    fact_hint: str | None = None
    # 批次九 D1：身份问句兜底召回已触发（含零命中情形），渲染层据此输出
    # “没检索到 ≠ 从未记录过”的如实回答指引。
    identity_probe: bool = False


@dataclass(frozen=True, slots=True)
class RetrievalCandidate:
    entry: MemoryEntry
    embedding: list[float] | None
