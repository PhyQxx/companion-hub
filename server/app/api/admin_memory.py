from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field

from app.memory import (
    MemoryCandidate,
    MemoryEntry,
    MemoryOriginKind,
    MemoryRetriever,
    MemorySourceKind,
    MemorySourceRef,
    MemoryStatus,
    MemoryStore,
    MemorySubjectKind,
    MemoryType,
    replay_deletions,
)
from app.schemas.common import PrivacyLevel, StrictModel

from .admin_config import AdminTokenGuard


class MemoryView(StrictModel):
    id: int
    user_id: UUID
    subject: MemorySubjectKind
    subject_key: str
    fact_key: str | None
    origin_kind: MemoryOriginKind
    type: MemoryType
    content: str
    summary: str | None
    privacy_level: PrivacyLevel
    importance: float
    pin: bool
    status: MemoryStatus
    confidence: float | None
    valid_from: datetime | None
    valid_to: datetime | None
    superseded_by: int | None
    supersede_reason: str | None
    conflict_with: int | None
    extractor_version: str | None
    created_by: str
    created_at: datetime
    updated_at: datetime
    last_accessed_at: datetime | None
    access_count: int
    embedding_model: str | None
    embedding_dimension: int | None
    embedding_version: str | None


class MemorySourceView(StrictModel):
    source_kind: MemorySourceKind
    source_id: str
    excerpt_hash: str | None


class MemoryDetailView(MemoryView):
    sources: list[MemorySourceView]
    lineage: list[MemoryView]


class ManualMemoryCreate(StrictModel):
    user_id: UUID
    subject: MemorySubjectKind = MemorySubjectKind.USER
    subject_key: Annotated[str | None, Field(min_length=1, max_length=160)] = None
    fact_key: Annotated[str | None, Field(min_length=1, max_length=160)] = None
    type: MemoryType
    content: Annotated[str, Field(min_length=2, max_length=2_000)]
    summary: Annotated[str | None, Field(max_length=2_000)] = None
    privacy_level: PrivacyLevel = PrivacyLevel.L1
    importance: Annotated[float, Field(ge=0, le=1)] = 0.6
    pin: bool = False
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class MemoryEditRequest(StrictModel):
    content: Annotated[str | None, Field(min_length=2, max_length=2_000)] = None
    summary: Annotated[str | None, Field(max_length=2_000)] = None
    importance: Annotated[float | None, Field(ge=0, le=1)] = None
    pin: bool | None = None
    valid_to: datetime | None = None
    reason: Annotated[str, Field(min_length=1, max_length=400)]


class ConflictResolveRequest(StrictModel):
    action: Literal["adopt", "keep"]


class RetrievalQueryRequest(StrictModel):
    user_id: UUID
    query: Annotated[str, Field(min_length=1, max_length=2_000)]
    privacy_level: PrivacyLevel = PrivacyLevel.L1


class MemoryHitView(StrictModel):
    memory: MemoryView
    vector_score: float
    lexical_score: float
    final_score: float
    reasons: list[str]


class RetrievalQueryResult(StrictModel):
    policy_version: str
    candidate_count: int
    hits: list[MemoryHitView]


class DeletionReceiptView(StrictModel):
    ledger_id: int
    entity_id: str
    deleted_ids: list[int]


class DeletionLedgerView(StrictModel):
    id: int
    entity_kind: str
    entity_id: str
    deleted_ids: list[int]
    requested_by: str
    reason: str | None
    created_at: datetime


class MemoryListResponse(StrictModel):
    items: list[MemoryView]
    total: int
    limit: int
    offset: int


class DeletionLedgerListResponse(StrictModel):
    items: list[DeletionLedgerView]
    total: int
    limit: int
    offset: int


class ReplayRequest(StrictModel):
    dry_run: bool = True


class ReplayResponse(StrictModel):
    ledger_rows: int
    conversations_deleted: int
    memories_deleted: int
    dry_run: bool


def _view(entry: MemoryEntry) -> MemoryView:
    return MemoryView(
        id=entry.id,
        user_id=entry.user_id,
        subject=MemorySubjectKind(entry.subject_kind),
        subject_key=entry.subject_key,
        fact_key=entry.fact_key,
        origin_kind=MemoryOriginKind(entry.origin_kind),
        type=MemoryType(entry.type),
        content=entry.content,
        summary=entry.summary,
        privacy_level=PrivacyLevel(entry.privacy_level),
        importance=entry.importance,
        pin=entry.pin,
        status=MemoryStatus(entry.status),
        confidence=entry.confidence,
        valid_from=entry.valid_from,
        valid_to=entry.valid_to,
        superseded_by=entry.superseded_by,
        supersede_reason=entry.supersede_reason,
        conflict_with=entry.conflict_with,
        extractor_version=entry.extractor_version,
        created_by=entry.created_by,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        last_accessed_at=entry.last_accessed_at,
        access_count=entry.access_count,
        embedding_model=entry.embedding_model,
        embedding_dimension=entry.embedding_dimension,
        embedding_version=entry.embedding_version,
    )


def _default_subject_key(subject: MemorySubjectKind) -> str:
    """给管理端省略 subject_key 的常见单主体场景提供稳定默认值。"""

    subject_kind = MemorySubjectKind(subject)
    if subject_kind is MemorySubjectKind.ASSISTANT:
        return "assistant:primary"
    if subject_kind is MemorySubjectKind.SHARED:
        return "shared:user-assistant"
    return "user:self"


def create_admin_memory_router(store: MemoryStore, *, admin_token: str | None) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/memories",
        tags=["admin-memories"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    @router.post("/query", response_model=RetrievalQueryResult)
    async def query_memories(payload: RetrievalQueryRequest) -> RetrievalQueryResult:
        result = await MemoryRetriever(store).retrieve(
            payload.query,
            user_id=payload.user_id,
            privacy_level=payload.privacy_level,
        )
        return RetrievalQueryResult(
            policy_version=result.policy_version,
            candidate_count=result.candidate_count,
            hits=[
                MemoryHitView(
                    memory=_view(hit.memory),
                    vector_score=round(hit.vector_score, 4),
                    lexical_score=round(hit.lexical_score, 4),
                    final_score=round(hit.final_score, 4),
                    reasons=list(hit.reasons),
                )
                for hit in result.hits
            ],
        )

    @router.get("", response_model=MemoryListResponse)
    async def list_memories(
        user_id: UUID | None = None,
        subject: MemorySubjectKind | None = None,
        subject_key: str | None = None,
        fact_key: str | None = None,
        origin_kind: MemoryOriginKind | None = None,
        type: MemoryType | None = None,
        memory_status: Annotated[MemoryStatus | None, Query(alias="status")] = None,
        min_importance: float | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> MemoryListResponse:
        total = await store.count_memories(
            user_id=user_id,
            subject_kind=subject,
            subject_key=subject_key,
            fact_key=fact_key,
            origin_kind=origin_kind,
            type=type,
            status=memory_status,
            min_importance=min_importance,
        )
        entries = await store.list_memories(
            user_id=user_id,
            subject_kind=subject,
            subject_key=subject_key,
            fact_key=fact_key,
            origin_kind=origin_kind,
            type=type,
            status=memory_status,
            min_importance=min_importance,
            limit=limit,
            offset=offset,
        )
        return MemoryListResponse(
            items=[_view(entry) for entry in entries],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.post("", response_model=MemoryView, status_code=status.HTTP_201_CREATED)
    async def create_memory(payload: ManualMemoryCreate) -> MemoryView:
        if payload.privacy_level == PrivacyLevel.L3:
            raise HTTPException(422, "L3 cannot be stored")
        candidate = MemoryCandidate(
            subject_kind=payload.subject,
            subject_key=payload.subject_key or _default_subject_key(payload.subject),
            fact_key=payload.fact_key,
            origin_kind=MemoryOriginKind.MANUAL,
            type=payload.type,
            content=payload.content,
            privacy_level=payload.privacy_level,
            sources=[
                MemorySourceRef(
                    source_kind=MemorySourceKind.MANUAL,
                    source_id=f"admin:{uuid.uuid4().hex[:12]}",
                )
            ],
            importance=payload.importance,
            pin=payload.pin,
            summary=payload.summary,
            extractor_version="manual",
            valid_from=payload.valid_from,
            valid_to=payload.valid_to,
        )
        entry = await store.add(candidate, user_id=payload.user_id, actor="admin")
        return _view(entry)

    @router.get("/{memory_id}", response_model=MemoryDetailView)
    async def memory_detail(memory_id: int) -> MemoryDetailView:
        try:
            entry = await store.get(memory_id)
            sources = await store.get_sources(memory_id)
            lineage = await store.lineage(memory_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        base = _view(entry).model_dump()
        return MemoryDetailView(
            **base,
            sources=[
                MemorySourceView(
                    source_kind=MemorySourceKind(source.source_kind),
                    source_id=source.source_id,
                    excerpt_hash=source.excerpt_hash,
                )
                for source in sources
            ],
            lineage=[_view(item) for item in lineage],
        )

    @router.patch("/{memory_id}", response_model=MemoryView)
    async def edit_memory(memory_id: int, payload: MemoryEditRequest) -> MemoryView:
        try:
            entry = await store.edit(
                memory_id,
                content=payload.content,
                summary=payload.summary,
                importance=payload.importance,
                pin=payload.pin,
                valid_to=payload.valid_to,
                actor="admin",
                reason=payload.reason,
            )
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        return _view(entry)

    @router.post("/{memory_id}/archive", response_model=MemoryView)
    async def archive_memory(memory_id: int) -> MemoryView:
        try:
            entry = await store.set_status(memory_id, MemoryStatus.ARCHIVED)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        return _view(entry)

    @router.post("/{memory_id}/resolve", response_model=MemoryView)
    async def resolve_conflict(memory_id: int, payload: ConflictResolveRequest) -> MemoryView:
        try:
            entry = await store.resolve_conflict(
                memory_id, adopt=payload.action == "adopt", actor="admin"
            )
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        return _view(entry)

    @router.delete("/{memory_id}", response_model=DeletionReceiptView)
    async def hard_delete_memory(
        memory_id: int, reason: Annotated[str | None, Query(max_length=400)] = None
    ) -> DeletionReceiptView:
        try:
            receipt = await store.hard_delete(memory_id, actor="admin", reason=reason)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        return DeletionReceiptView(
            ledger_id=receipt.ledger_id,
            entity_id=receipt.entity_id,
            deleted_ids=list(receipt.deleted_ids),
        )

    return router


def create_deletion_ledger_router(store: MemoryStore, *, admin_token: str | None) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/deletion-ledger",
        tags=["admin-memories"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    @router.get("", response_model=DeletionLedgerListResponse)
    async def ledger_entries(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> DeletionLedgerListResponse:
        return DeletionLedgerListResponse(
            items=[
                DeletionLedgerView(
                    id=item.id,
                    entity_kind=item.entity_kind,
                    entity_id=item.entity_id,
                    deleted_ids=list(item.deleted_ids),
                    requested_by=item.requested_by,
                    reason=item.reason,
                    created_at=item.created_at,
                )
                for item in await store.list_deletion_ledger(limit=limit, offset=offset)
            ],
            total=await store.count_deletion_ledger(),
            limit=limit,
            offset=offset,
        )

    @router.post("/replay", response_model=ReplayResponse)
    async def replay(payload: ReplayRequest) -> ReplayResponse:
        report = await replay_deletions(store.database, dry_run=payload.dry_run)
        return ReplayResponse(
            ledger_rows=report.ledger_rows,
            conversations_deleted=report.conversations_deleted,
            memories_deleted=report.memories_deleted,
            dry_run=report.dry_run,
        )

    return router
