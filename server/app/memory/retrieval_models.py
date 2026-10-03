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
