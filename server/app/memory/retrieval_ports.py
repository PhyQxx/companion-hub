"""Query contracts for retrieval policy; no storage or model implementation."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.schemas.common import PrivacyLevel

from .models import MemoryEntry, MemoryStatus, MemorySubjectKind
from .retrieval_models import RetrievalCandidate


class EmbeddingQueryPort(Protocol):
    @property
    def version(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class MemoryRetrievalRepository(Protocol):
    @property
    def embedding_provider(self) -> EmbeddingQueryPort: ...

    @property
    def vector_sql_enabled(self) -> bool: ...

    async def retrieval_candidates(
        self,
        user_id: UUID,
        *,
        subject_scopes: Sequence[tuple[MemorySubjectKind, str]],
        statuses: Sequence[MemoryStatus],
        privacy_levels: Sequence[PrivacyLevel],
        valid_at: datetime,
        limit: int,
    ) -> list[RetrievalCandidate]: ...

    async def fact_candidates(
        self,
        *,
        user_id: UUID,
        fact_key: str,
        subject_scopes: Sequence[tuple[MemorySubjectKind, str]],
        privacy_levels: Sequence[PrivacyLevel],
        valid_at: datetime,
    ) -> list[MemoryEntry]: ...

    async def vector_recall(
        self,
        query_vector: Sequence[float],
        *,
        user_id: UUID,
        embedding_version: str,
        privacy_levels: Sequence[str],
        subject_keys: Sequence[str],
        valid_at: datetime,
        limit: int,
    ) -> list[tuple[int, float]]: ...

    async def record_access(self, memory_ids: Sequence[int]) -> None: ...
