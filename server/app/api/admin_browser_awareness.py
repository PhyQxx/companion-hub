from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from app.browser_awareness import BrowserAwarenessLoop
from app.db import AppUserRecord
from app.schemas.common import PrivacyLevel, StrictModel
from app.timeline.models import TimelineSourceType
from app.timeline.store import TimelineStore

from .admin_config import AdminTokenGuard


class BrowserAwarenessStatus(StrictModel):
    configured_enabled: bool
    interval_seconds: int
    memory_enabled: bool
    proactive_enabled: bool
    max_text_chars: int
    blocked_hosts: list[str]
    loop_running: bool
    cycles: int
    observations: int
    last_tick_at: datetime | None
    last_origin: str | None
    last_hash: str | None
    last_analyzed_at: datetime | None
    last_summary: str | None
    consecutive_failures: int
    cooldown_until: datetime | None
    last_error: str | None


class BrowserObservationItem(StrictModel):
    id: int
    occurred_at: datetime
    origin: str
    page_title: str
    title: str
    summary: str
    importance: float
    metadata: dict[str, Any]


class BrowserObservationsResponse(StrictModel):
    items: list[BrowserObservationItem]
    total: int
    limit: int
    offset: int


def _loop_from(request: Request) -> BrowserAwarenessLoop | None:
    loop = getattr(request.app.state, "browser_awareness_loop", None)
    return loop if isinstance(loop, BrowserAwarenessLoop) else None


def create_admin_browser_awareness_router(
    timeline: TimelineStore | None, *, admin_token: str | None
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/browser-awareness",
        tags=["admin-browser-awareness"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    @router.get("/status", response_model=BrowserAwarenessStatus)
    async def status(request: Request) -> BrowserAwarenessStatus:
        loop = _loop_from(request)
        if loop is None:
            return BrowserAwarenessStatus(
                configured_enabled=False,
                interval_seconds=60,
                memory_enabled=False,
                proactive_enabled=False,
                max_text_chars=8_000,
                blocked_hosts=[],
                loop_running=False,
                cycles=0,
                observations=0,
                last_tick_at=None,
                last_origin=None,
                last_hash=None,
                last_analyzed_at=None,
                last_summary=None,
                consecutive_failures=0,
                cooldown_until=None,
                last_error="browser awareness not assembled (requires database config)",
            )
        config = loop._config_store.current.config.browser_awareness
        state = loop.state
        return BrowserAwarenessStatus(
            configured_enabled=config.enabled,
            interval_seconds=config.interval_seconds,
            memory_enabled=config.memory_enabled,
            proactive_enabled=config.proactive_enabled,
            max_text_chars=config.max_text_chars,
            blocked_hosts=list(config.blocked_hosts),
            loop_running=state.running,
            cycles=state.cycles,
            observations=state.observations,
            last_tick_at=state.last_tick_at,
            last_origin=state.last_origin,
            last_hash=state.last_hash,
            last_analyzed_at=state.last_analyzed_at,
            last_summary=state.last_summary,
            consecutive_failures=state.consecutive_failures,
            cooldown_until=state.cooldown_until,
            last_error=state.last_error,
        )

    @router.get("/observations", response_model=BrowserObservationsResponse)
    async def observations(
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> BrowserObservationsResponse:
        if timeline is None:
            return BrowserObservationsResponse(items=[], total=0, limit=limit, offset=offset)
        async with timeline.database.sessions() as session:
            owner = await session.scalar(
                select(AppUserRecord.id).order_by(AppUserRecord.created_at)
            )
        if owner is None:
            return BrowserObservationsResponse(items=[], total=0, limit=limit, offset=offset)
        filters = {
            "source_types": (TimelineSourceType.DEVICE,),
            "event_types": ("browser.observed",),
            "privacy_levels": (PrivacyLevel.L0, PrivacyLevel.L1),
        }
        total = await timeline.count_events(user_id=UUID(str(owner)), **filters)
        result = await timeline.search(user_id=UUID(str(owner)), **filters,
                                       limit=limit, offset=offset)
        items = [
            BrowserObservationItem(
                id=event.id,
                occurred_at=event.occurred_at,
                origin=str((event.metadata or {}).get("origin", "")),
                page_title=str((event.metadata or {}).get("page_title", "")),
                title=event.title or "浏览观察",
                summary=event.summary,
                importance=event.importance,
                metadata=dict(event.metadata or {}),
            )
            for event in result.events
        ]
        return BrowserObservationsResponse(items=items, total=total, limit=limit, offset=offset)

    return router
