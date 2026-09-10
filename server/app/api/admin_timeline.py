from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field

from app.schemas.common import PrivacyLevel, StrictModel
from app.timeline import TimelineActor, TimelineEvent, TimelineSourceType, TimelineStore

from .admin_config import AdminTokenGuard


class TimelineView(StrictModel):
    id: int
    user_id: UUID
    occurred_at: datetime
    ended_at: datetime | None
    source_type: TimelineSourceType
    source_id: str
    actor: TimelineActor
    event_type: str
    conversation_id: UUID | None
    title: str | None
    summary: str
    privacy_level: PrivacyLevel
    importance: float
    entities: list[dict[str, object]]
    keywords: list[str]
    metadata: dict[str, object]
    created_at: datetime


class TimelineQueryRequest(StrictModel):
    user_id: UUID
    query: Annotated[str, Field(max_length=2_000)] = ""
    start_at: datetime | None = None
    end_at: datetime | None = None
    actors: list[TimelineActor] = Field(default_factory=list, max_length=8)
    source_types: list[TimelineSourceType] = Field(default_factory=list, max_length=8)
    event_types: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(
        default_factory=list, max_length=16
    )
    conversation_id: UUID | None = None
    privacy_level: PrivacyLevel = PrivacyLevel.L1
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    offset: Annotated[int, Field(ge=0)] = 0


class TimelineQueryResult(StrictModel):
    candidate_count: int
    total: int
    limit: int
    offset: int
    events: list[TimelineView]


class TimelineSourceView(StrictModel):
    timeline_id: int
    source_type: TimelineSourceType
    source_id: str
    occurred_at: datetime
    actor: TimelineActor
    event_type: str
    text: str


def _view(entry: TimelineEvent) -> TimelineView:
    return TimelineView(
        id=entry.id,
        user_id=entry.user_id,
        occurred_at=entry.occurred_at,
        ended_at=entry.ended_at,
        source_type=TimelineSourceType(entry.source_type),
        source_id=entry.source_id,
        actor=TimelineActor(entry.actor),
        event_type=entry.event_type,
        conversation_id=entry.conversation_id,
        title=entry.title,
        summary=entry.summary,
        privacy_level=PrivacyLevel(entry.privacy_level),
        importance=entry.importance,
        entities=list(entry.entities),
        keywords=list(entry.keywords),
        metadata=dict(entry.metadata),
        created_at=entry.created_at,
    )


def create_admin_timeline_router(
    store: TimelineStore, *, admin_token: str | None
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/timeline",
        tags=["admin-timeline"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    @router.get("", response_model=list[TimelineView])
    async def list_timeline(
        user_id: UUID,
        query: str = "",
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        actor: TimelineActor | None = None,
        source_type: TimelineSourceType | None = None,
        event_type: str | None = None,
        privacy_level: PrivacyLevel = PrivacyLevel.L1,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[TimelineView]:
        levels = (
            (PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2)
            if privacy_level is PrivacyLevel.L2
            else (PrivacyLevel.L0, PrivacyLevel.L1)
        )
        result = await store.search(
            user_id=user_id,
            query=query,
            start_at=start_at,
            end_at=end_at,
            actors=[actor] if actor else None,
            source_types=[source_type] if source_type else None,
            event_types=[event_type] if event_type else None,
            privacy_levels=levels,
            limit=limit,
        )
        return [_view(item) for item in result.events]

    @router.post("/query", response_model=TimelineQueryResult)
    async def query_timeline(payload: TimelineQueryRequest) -> TimelineQueryResult:
        levels = (
            (PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2)
            if payload.privacy_level == PrivacyLevel.L2
            else (PrivacyLevel.L0, PrivacyLevel.L1)
        )
        filters = {
            "start_at": payload.start_at,
            "end_at": payload.end_at,
            "actors": payload.actors or None,
            "source_types": payload.source_types or None,
            "event_types": payload.event_types or None,
            "conversation_id": payload.conversation_id,
            "privacy_levels": levels,
        }
        result = await store.search(
            user_id=payload.user_id,
            query=payload.query,
            **filters,
            limit=payload.limit,
            offset=payload.offset,
        )
        # 文本相关性过滤在内存打分，无法精确计数；无文本时走 SQL 精确总数
        if payload.query.strip():
            total = result.candidate_count
        else:
            total = await store.count_events(user_id=payload.user_id, **filters)
        return TimelineQueryResult(
            candidate_count=result.candidate_count,
            total=total,
            limit=payload.limit,
            offset=payload.offset,
            events=[_view(item) for item in result.events],
        )

    @router.get("/{timeline_id}", response_model=TimelineView)
    async def timeline_detail(timeline_id: int) -> TimelineView:
        try:
            return _view(await store.get(timeline_id))
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @router.get("/{timeline_id}/source", response_model=TimelineSourceView)
    async def timeline_source(timeline_id: int) -> TimelineSourceView:
        try:
            entry = await store.get(timeline_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        evidence = await store.expand_sources(
            [entry],
            user_id=entry.user_id,
            privacy_level=PrivacyLevel(entry.privacy_level),
            limit=1,
        )
        if not evidence:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail="timeline source is unavailable or not expandable",
            )
        item = evidence[0]
        return TimelineSourceView(
            timeline_id=item.timeline_id,
            source_type=TimelineSourceType(item.source_type),
            source_id=item.source_id,
            occurred_at=item.occurred_at,
            actor=TimelineActor(item.actor),
            event_type=item.event_type,
            text=item.text,
        )

    return router
