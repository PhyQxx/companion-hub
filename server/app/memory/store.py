from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import bindparam, delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import (
    Database,
    DeletionLedgerRecord,
    MemoryRecord,
    MemorySourceRecord,
)
from app.schemas.common import PrivacyLevel

from .embeddings import EmbeddingProvider, HashingEmbeddingProvider, cosine_similarity
from .models import (
    DeletionLedgerEntry,
    DeletionReceipt,
    MemoryCandidate,
    MemoryEntry,
    MemorySourceEntry,
    MemorySourceKind,
    MemorySourceRef,
    MemoryStatus,
    MemoryType,
    SimilarMemory,
)


@dataclass(frozen=True, slots=True)
class RetrievalCandidate:
    entry: MemoryEntry
    embedding: list[float] | None


class MemoryStore:
    def __init__(
        self,
        database: Database,
        *,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self._database = database
        self._embedding_provider: EmbeddingProvider = (
            embedding_provider or HashingEmbeddingProvider()
        )

    @property
    def database(self) -> Database:
        return self._database

    @property
    def vector_sql_enabled(self) -> bool:
        """True when the pgvector column exists (PostgreSQL after 0009)."""
        return self._database.engine.dialect.name == "postgresql"

    @property
    def embedding_provider(self) -> EmbeddingProvider:
        return self._embedding_provider

    async def add(
        self,
        candidate: MemoryCandidate,
        *,
        user_id: UUID,
        actor: str = "system",
        status: MemoryStatus = MemoryStatus.ACTIVE,
        conflict_with: int | None = None,
    ) -> MemoryEntry:
        privacy = PrivacyLevel(candidate.privacy_level)
        if privacy is PrivacyLevel.L3:
            raise ValueError("L3 content must never become a durable memory")
        vector = (await self._embedding_provider.embed([candidate.content]))[0]
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            record = MemoryRecord(
                user_id=user_id,
                type=MemoryType(candidate.type).value,
                content=candidate.content,
                summary=candidate.summary,
                privacy_level=privacy.value,
                embedding=vector,
                embedding_model=self._embedding_provider.model_name,
                embedding_dimension=self._embedding_provider.dimension,
                embedding_version=self._embedding_provider.version,
                importance=min(1.0, max(0.0, candidate.importance)),
                pin=candidate.pin,
                status=status.value,
                confidence=candidate.confidence,
                valid_from=candidate.valid_from or now,
                valid_to=candidate.valid_to,
                conflict_with=conflict_with,
                extractor_version=candidate.extractor_version,
                created_by=actor,
                created_at=now,
                updated_at=now,
            )
            session.add(record)
            await session.flush()
            await self._write_sources(session, record.id, candidate.sources)
            await self._sync_vector_column(session, record.id, vector)
            await session.refresh(record)
        return self._entry(record)

    async def get(self, memory_id: int, *, user_id: UUID | None = None) -> MemoryEntry:
        async with self._database.sessions() as session:
            record = await session.get(MemoryRecord, memory_id)
        if record is None or (user_id is not None and record.user_id != user_id):
            raise LookupError(f"memory not found: {memory_id}")
        return self._entry(record)

    async def get_sources(self, memory_id: int) -> list[MemorySourceEntry]:
        async with self._database.sessions() as session:
            records = list(
                await session.scalars(
                    select(MemorySourceRecord)
                    .where(MemorySourceRecord.memory_id == memory_id)
                    .order_by(MemorySourceRecord.created_at, MemorySourceRecord.source_id)
                )
            )
        return [
            MemorySourceEntry(
                source_kind=record.source_kind,
                source_id=record.source_id,
                excerpt_hash=record.excerpt_hash,
            )
            for record in records
        ]

    async def list_memories(
        self,
        *,
        user_id: UUID | None = None,
        type: MemoryType | None = None,
        status: MemoryStatus | None = None,
        min_importance: float | None = None,
        limit: int = 100,
    ) -> list[MemoryEntry]:
        query = select(MemoryRecord).order_by(
            MemoryRecord.importance.desc(), MemoryRecord.id.desc()
        )
        if user_id is not None:
            query = query.where(MemoryRecord.user_id == user_id)
        if type is not None:
            query = query.where(MemoryRecord.type == type.value)
        if status is not None:
            query = query.where(MemoryRecord.status == status.value)
        if min_importance is not None:
            query = query.where(MemoryRecord.importance >= min_importance)
        query = query.limit(limit)
        async with self._database.sessions() as session:
            records = list(await session.scalars(query))
        return [self._entry(record) for record in records]

    async def retrieval_candidates(
        self,
        user_id: UUID,
        *,
        types: Sequence[MemoryType] | None = None,
        statuses: Sequence[MemoryStatus] = (MemoryStatus.ACTIVE,),
        privacy_levels: Sequence[PrivacyLevel] | None = None,
        valid_at: datetime | None = None,
        limit: int = 500,
    ) -> list[RetrievalCandidate]:
        query = select(MemoryRecord)
        if types:
            query = query.where(MemoryRecord.type.in_([item.value for item in types]))
        query = query.where(
            MemoryRecord.status.in_([item.value for item in statuses]),
            MemoryRecord.user_id == user_id,
        )
        if privacy_levels:
            query = query.where(
                MemoryRecord.privacy_level.in_([item.value for item in privacy_levels])
            )
        if valid_at is not None:
            query = query.where(
                (MemoryRecord.valid_from.is_(None) | (MemoryRecord.valid_from <= valid_at)),
                (MemoryRecord.valid_to.is_(None) | (MemoryRecord.valid_to > valid_at)),
            )
        query = query.order_by(MemoryRecord.importance.desc()).limit(limit)
        async with self._database.sessions() as session:
            records = list(await session.scalars(query))
        return [
            RetrievalCandidate(entry=self._entry(record), embedding=record.embedding)
            for record in records
        ]

    async def vector_recall(
        self,
        query_vector: Sequence[float],
        *,
        user_id: UUID,
        embedding_version: str,
        privacy_levels: Sequence[str],
        valid_at: datetime,
        limit: int,
    ) -> list[tuple[int, float]]:
        """pgvector ANN recall; empty on dialects without the vector column."""
        if not self.vector_sql_enabled or not privacy_levels:
            return []
        statement = text(
            """
            SELECT id, 1 - (embedding_vec <=> CAST(:qv AS vector)) AS similarity
            FROM memory
            WHERE user_id = :uid
              AND status = 'active'
              AND embedding_version = :version
              AND privacy_level IN :levels
              AND (valid_from IS NULL OR valid_from <= :now)
              AND (valid_to IS NULL OR valid_to > :now)
              AND embedding_vec IS NOT NULL
            ORDER BY embedding_vec <=> CAST(:qv AS vector)
            LIMIT :lim
            """
        ).bindparams(bindparam("levels", expanding=True))
        async with self._database.sessions() as session:
            rows = (
                await session.execute(
                    statement,
                    {
                        "qv": _vector_literal(query_vector),
                        "uid": user_id,
                        "version": embedding_version,
                        "levels": list(privacy_levels),
                        "now": valid_at,
                        "lim": limit,
                    },
                )
            ).all()
        return [(int(row[0]), float(row[1])) for row in rows]

    async def find_similar(
        self,
        content: str,
        *,
        user_id: UUID,
        type: MemoryType,
        statuses: Sequence[MemoryStatus] = (MemoryStatus.ACTIVE,),
        limit: int = 5,
    ) -> list[SimilarMemory]:
        vector = (await self._embedding_provider.embed([content]))[0]
        candidates = await self.retrieval_candidates(
            user_id,
            types=[type],
            statuses=statuses,
            limit=500,
        )
        scored = [
            SimilarMemory(entry=item.entry, similarity=cosine_similarity(vector, item.embedding))
            for item in candidates
            if item.embedding is not None
            and item.entry.embedding_version == self._embedding_provider.version
        ]
        scored.sort(key=lambda item: item.similarity, reverse=True)
        return scored[:limit]

    async def register_support(
        self,
        memory_id: int,
        *,
        sources: Sequence[MemorySourceRef],
        importance_step: float,
    ) -> MemoryEntry:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            record = await session.get(MemoryRecord, memory_id, with_for_update=True)
            if record is None:
                raise LookupError(f"memory not found: {memory_id}")
            record.importance = min(1.0, record.importance + importance_step)
            record.access_count += 1
            record.last_accessed_at = now
            record.updated_at = now
            await self._write_sources(session, record.id, sources)
        return self._entry(record)

    async def record_access(self, memory_ids: Sequence[int]) -> None:
        if not memory_ids:
            return
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            records = list(
                await session.scalars(
                    select(MemoryRecord)
                    .where(MemoryRecord.id.in_(list(memory_ids)))
                    .with_for_update()
                )
            )
            for record in records:
                record.access_count += 1
                record.last_accessed_at = now

    async def set_status(
        self,
        memory_id: int,
        status: MemoryStatus,
        *,
        user_id: UUID | None = None,
        reason: str | None = None,
    ) -> MemoryEntry:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            record = await session.get(MemoryRecord, memory_id, with_for_update=True)
            if record is None or (user_id is not None and record.user_id != user_id):
                raise LookupError(f"memory not found: {memory_id}")
            if record.status == MemoryStatus.SUPERSEDED.value:
                raise ValueError("a superseded memory cannot change status")
            record.status = status.value
            record.updated_at = now
            if status is MemoryStatus.ARCHIVED and reason:
                record.supersede_reason = reason
        return self._entry(record)

    async def edit(
        self,
        memory_id: int,
        *,
        user_id: UUID | None = None,
        content: str | None = None,
        summary: str | None = None,
        importance: float | None = None,
        pin: bool | None = None,
        valid_to: datetime | None = None,
        actor: str,
        reason: str,
    ) -> MemoryEntry:
        """Creates a corrected version that supersedes the original.

        The original row stays queryable for traceability: it keeps its
        content, gains status superseded and points at the replacement.
        Omitted fields carry over from the current version.
        """
        current = await self.get(memory_id, user_id=user_id)
        if current.status == MemoryStatus.SUPERSEDED.value:
            raise ValueError("a superseded memory cannot be edited")
        new_content = content if content is not None else current.content
        if not new_content.strip():
            raise ValueError("memory content must not be empty")
        now = datetime.now(UTC)
        vector = (await self._embedding_provider.embed([new_content]))[0]
        async with self._database.sessions.begin() as session:
            old = await session.get(MemoryRecord, memory_id, with_for_update=True)
            if old is None or old.status == MemoryStatus.SUPERSEDED.value:
                raise LookupError(f"memory not found: {memory_id}")
            replacement = MemoryRecord(
                user_id=old.user_id,
                type=old.type,
                content=new_content,
                summary=summary if summary is not None else old.summary,
                privacy_level=old.privacy_level,
                embedding=vector,
                embedding_model=self._embedding_provider.model_name,
                embedding_dimension=self._embedding_provider.dimension,
                embedding_version=self._embedding_provider.version,
                importance=importance if importance is not None else old.importance,
                pin=pin if pin is not None else old.pin,
                status=MemoryStatus.ACTIVE.value,
                confidence=old.confidence,
                valid_from=old.valid_from or now,
                valid_to=valid_to if valid_to is not None else old.valid_to,
                extractor_version=old.extractor_version or "manual",
                created_by=actor,
                created_at=now,
                updated_at=now,
            )
            session.add(replacement)
            await session.flush()
            old_sources = list(
                await session.scalars(
                    select(MemorySourceRecord).where(MemorySourceRecord.memory_id == memory_id)
                )
            )
            await self._sync_vector_column(session, replacement.id, vector)
            for source in old_sources:
                session.add(
                    MemorySourceRecord(
                        memory_id=replacement.id,
                        source_kind=source.source_kind,
                        source_id=source.source_id,
                        excerpt_hash=source.excerpt_hash,
                    )
                )
            session.add(
                MemorySourceRecord(
                    memory_id=replacement.id,
                    source_kind="memory",
                    source_id=str(memory_id),
                )
            )
            old.status = MemoryStatus.SUPERSEDED.value
            old.superseded_by = replacement.id
            old.supersede_reason = reason
            old.updated_at = now
        return self._entry(replacement)

    async def resolve_conflict(
        self, memory_id: int, *, adopt: bool, actor: str
    ) -> MemoryEntry:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            record = await session.get(MemoryRecord, memory_id, with_for_update=True)
            if record is None:
                raise LookupError(f"memory not found: {memory_id}")
            if record.status != MemoryStatus.CONFLICT.value:
                raise ValueError("only a conflicted memory can be resolved")
            target = (
                await session.get(MemoryRecord, record.conflict_with or -1, with_for_update=True)
                if record.conflict_with is not None
                else None
            )
            if adopt:
                record.status = MemoryStatus.ACTIVE.value
                record.conflict_with = None
                record.updated_at = now
                if target is not None:
                    target.status = MemoryStatus.SUPERSEDED.value
                    target.superseded_by = record.id
                    target.supersede_reason = f"conflict resolved by {actor}: adopted new"
                    target.updated_at = now
            else:
                record.status = MemoryStatus.ARCHIVED.value
                record.conflict_with = None
                record.supersede_reason = f"conflict resolved by {actor}: kept existing"
                record.updated_at = now
        return self._entry(record)

    async def hard_delete(
        self, memory_id: int, *, actor: str, reason: str | None = None
    ) -> DeletionReceipt:
        """Removes the full version chain of a memory and records the ledger.

        Deletion always covers the whole supersede lineage plus every stored
        source reference; surviving memories that link to a deleted version
        (derived `memory` sources or `conflict_with` pointers) are detached
        inside the same transaction. The ledger row keeps identifiers only.
        """
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            record = await session.get(MemoryRecord, memory_id)
            if record is None:
                raise LookupError(f"memory not found: {memory_id}")
            deleted_ids = await self._collect_lineage_ids(session, [record.id])
            await self._purge_memory_ids(session, deleted_ids)
            ledger_id = await self._record_ledger(
                session, "memory", str(memory_id), deleted_ids, actor, reason, now
            )
        return DeletionReceipt(
            ledger_id=ledger_id,
            entity_id=str(memory_id),
            deleted_ids=tuple(deleted_ids),
        )

    async def memory_ids_by_source(
        self, source_kind: str, source_ids: Sequence[str]
    ) -> list[int]:
        source_id_list = list(source_ids)
        if not source_id_list:
            return []
        async with self._database.sessions() as session:
            return [
                row
                for row in await session.scalars(
                    select(MemorySourceRecord.memory_id).where(
                        MemorySourceRecord.source_kind == source_kind,
                        MemorySourceRecord.source_id.in_(source_id_list),
                    )
                )
            ]

    async def hard_delete_by_source(
        self,
        source_kind: str,
        source_ids: Sequence[str],
        *,
        entity_kind: str,
        entity_id: str,
        actor: str,
        reason: str | None = None,
        always_record: bool = False,
    ) -> DeletionReceipt:
        """Deletes every memory chain rooted at the given source references.

        Used by conversation deletion: entity_kind='message' with the
        conversation id gives the ledger a stable replay handle even when no
        memory was ever consolidated from those messages.
        """
        now = datetime.now(UTC)
        seed_ids = await self.memory_ids_by_source(source_kind, source_ids)
        async with self._database.sessions.begin() as session:
            deleted_ids = (
                await self._collect_lineage_ids(session, seed_ids) if seed_ids else []
            )
            if deleted_ids:
                await self._purge_memory_ids(session, deleted_ids)
            if not deleted_ids and not always_record:
                return DeletionReceipt(
                    ledger_id=0, entity_id=entity_id, deleted_ids=()
                )
            ledger_id = await self._record_ledger(
                session, entity_kind, entity_id, deleted_ids, actor, reason, now
            )
        return DeletionReceipt(
            ledger_id=ledger_id,
            entity_id=entity_id,
            deleted_ids=tuple(deleted_ids),
        )

    async def purge_by_source(self, source_kind: str, source_ids: Sequence[str]) -> int:
        """Ledger-free variant used by the deletion replay tool."""
        source_id_list = list(source_ids)
        if not source_id_list:
            return 0
        async with self._database.sessions.begin() as session:
            seed_ids = [
                row
                for row in await session.scalars(
                    select(MemorySourceRecord.memory_id).where(
                        MemorySourceRecord.source_kind == source_kind,
                        MemorySourceRecord.source_id.in_(source_id_list),
                    )
                )
            ]
            if not seed_ids:
                return 0
            deleted_ids = await self._collect_lineage_ids(session, seed_ids)
            await self._purge_memory_ids(session, deleted_ids)
            return len(deleted_ids)

    async def purge_by_ids(self, memory_ids: Sequence[int]) -> int:
        """Ledger-free removal of whole chains by memory id (replay tool)."""
        if not memory_ids:
            return 0
        async with self._database.sessions.begin() as session:
            deleted_ids = await self._collect_lineage_ids(session, list(memory_ids))
            await self._purge_memory_ids(session, deleted_ids)
            return len(deleted_ids)

    async def existing_ids(self, memory_ids: Sequence[int]) -> list[int]:
        if not memory_ids:
            return []
        async with self._database.sessions() as session:
            return [
                row
                for row in await session.scalars(
                    select(MemoryRecord.id).where(
                        MemoryRecord.id.in_(list(memory_ids))
                    )
                )
            ]

    async def list_deletion_ledger(self, *, limit: int = 50) -> list[DeletionLedgerEntry]:
        async with self._database.sessions() as session:
            records = list(
                await session.scalars(
                    select(DeletionLedgerRecord)
                    .order_by(DeletionLedgerRecord.id.desc())
                    .limit(limit)
                )
            )
        return [
            DeletionLedgerEntry(
                id=item.id,
                entity_kind=item.entity_kind,
                entity_id=item.entity_id,
                deleted_ids=tuple(item.deleted_ids),
                requested_by=item.requested_by,
                reason=item.reason,
                created_at=cast(datetime, _aware(item.created_at)),
            )
            for item in records
        ]

    async def _collect_lineage_ids(
        self, session: AsyncSession, seed_ids: Sequence[int]
    ) -> list[int]:
        related: dict[int, MemoryRecord] = {}
        frontier: list[MemoryRecord] = []
        for seed_id in dict.fromkeys(seed_ids):
            record = await session.get(MemoryRecord, seed_id)
            if record is not None:
                frontier.append(record)
        while frontier:
            successors: list[MemoryRecord] = []
            for item in frontier:
                if item.id in related:
                    continue
                related[item.id] = item
                if item.superseded_by is not None:
                    successor = await session.get(MemoryRecord, item.superseded_by)
                    if successor is not None:
                        successors.append(successor)
                successors.extend(
                    await session.scalars(
                        select(MemoryRecord).where(MemoryRecord.superseded_by == item.id)
                    )
                )
            frontier = successors
        return sorted(related)

    async def _purge_memory_ids(
        self, session: AsyncSession, deleted_ids: Sequence[int]
    ) -> None:
        deleted_id_strings = [str(item) for item in deleted_ids]
        await session.execute(
            delete(MemorySourceRecord).where(
                MemorySourceRecord.source_kind == "memory",
                MemorySourceRecord.source_id.in_(deleted_id_strings),
            )
        )
        await session.execute(
            delete(MemorySourceRecord).where(MemorySourceRecord.memory_id.in_(deleted_ids))
        )
        await session.execute(
            update(MemoryRecord)
            .where(MemoryRecord.conflict_with.in_(deleted_ids))
            .values(conflict_with=None)
        )
        await session.execute(
            delete(MemoryRecord).where(MemoryRecord.id.in_(deleted_ids))
        )

    @staticmethod
    async def _record_ledger(
        session: AsyncSession,
        entity_kind: str,
        entity_id: str,
        deleted_ids: Sequence[int],
        actor: str,
        reason: str | None,
        now: datetime,
    ) -> int:
        ledger = DeletionLedgerRecord(
            entity_kind=entity_kind,
            entity_id=entity_id,
            deleted_ids=list(deleted_ids),
            requested_by=actor,
            reason=reason,
            created_at=now,
        )
        session.add(ledger)
        await session.flush()
        return ledger.id

    async def lineage(self, memory_id: int) -> list[MemoryEntry]:
        async with self._database.sessions() as session:
            record = await session.get(MemoryRecord, memory_id)
            if record is None:
                raise LookupError(f"memory not found: {memory_id}")
            related: dict[int, MemoryRecord] = {}
            frontier: list[MemoryRecord] = [record]
            while frontier:
                successors: list[MemoryRecord] = []
                for item in frontier:
                    if item.id in related:
                        continue
                    related[item.id] = item
                    if item.superseded_by is not None:
                        successor = await session.get(MemoryRecord, item.superseded_by)
                        if successor is not None:
                            successors.append(successor)
                    successors.extend(
                        await session.scalars(
                            select(MemoryRecord).where(MemoryRecord.superseded_by == item.id)
                        )
                    )
                frontier = successors
        return [self._entry(related[key]) for key in sorted(related)]

    async def _sync_vector_column(
        self, session: AsyncSession, memory_id: int, vector: Sequence[float]
    ) -> None:
        if not self.vector_sql_enabled:
            return
        await session.execute(
            text("UPDATE memory SET embedding_vec = CAST(:vec AS vector) WHERE id = :mid"),
            {"vec": _vector_literal(vector), "mid": memory_id},
        )

    async def _write_sources(
        self,
        session: AsyncSession,
        memory_id: int,
        sources: Sequence[MemorySourceRef],
    ) -> None:
        existing = {
            (row[0], row[1])
            for row in await session.execute(
                select(MemorySourceRecord.source_kind, MemorySourceRecord.source_id).where(
                    MemorySourceRecord.memory_id == memory_id
                )
            )
        }
        for source in sources:
            key = (MemorySourceKind(source.source_kind).value, source.source_id)
            if key in existing:
                continue
            existing.add(key)
            session.add(
                MemorySourceRecord(
                    memory_id=memory_id,
                    source_kind=MemorySourceKind(source.source_kind).value,
                    source_id=source.source_id,
                    excerpt_hash=_excerpt_hash(source.excerpt),
                )
            )

    @staticmethod
    def _entry(record: MemoryRecord) -> MemoryEntry:
        return MemoryEntry(
            id=record.id,
            user_id=record.user_id,
            type=record.type,
            content=record.content,
            summary=record.summary,
            privacy_level=record.privacy_level,
            importance=record.importance,
            pin=record.pin,
            status=record.status,
            confidence=record.confidence,
            valid_from=_aware(record.valid_from),
            valid_to=_aware(record.valid_to),
            superseded_by=record.superseded_by,
            supersede_reason=record.supersede_reason,
            conflict_with=record.conflict_with,
            extractor_version=record.extractor_version,
            created_by=record.created_by,
            created_at=cast(datetime, _aware(record.created_at)),
            updated_at=cast(datetime, _aware(record.updated_at)),
            last_accessed_at=_aware(record.last_accessed_at),
            access_count=record.access_count,
            embedding_model=record.embedding_model,
            embedding_dimension=record.embedding_dimension,
            embedding_version=record.embedding_version,
        )


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{value:.6f}" for value in vector) + "]"


def _excerpt_hash(excerpt: str | None) -> str | None:
    if excerpt is None:
        return None
    digest = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
