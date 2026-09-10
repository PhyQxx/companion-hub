from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.schemas.common import PrivacyLevel, StrictModel
from app.screen_awareness import ScreenAwarenessLoop
from app.timeline.models import TimelineSourceType
from app.timeline.store import TimelineStore

from .admin_config import AdminTokenGuard


class ScreenAwarenessDisplayState(StrictModel):
    display: int
    last_hash: str | None = None
    last_analyzed_at: datetime | None = None
    last_summary: str | None = None
    consecutive_failures: int = 0
    cooldown_until: datetime | None = None


class ScreenAwarenessStatus(StrictModel):
    configured_enabled: bool
    interval_seconds: int
    displays: list[int]
    memory_enabled: bool
    proactive_enabled: bool
    loop_running: bool
    cycles: int
    last_tick_at: datetime | None
    last_error: str | None
    display_states: list[ScreenAwarenessDisplayState]


class ScreenObservationItem(StrictModel):
    id: int
    occurred_at: datetime
    display: int
    title: str
    summary: str
    importance: float
    metadata: dict[str, Any]


class ScreenObservationsResponse(StrictModel):
    items: list[ScreenObservationItem]
    total: int
    limit: int
    offset: int


def _loop_from(request: Request) -> ScreenAwarenessLoop | None:
    loop = getattr(request.app.state, "screen_awareness_loop", None)
    return loop if isinstance(loop, ScreenAwarenessLoop) else None


def create_admin_screen_awareness_router(
    timeline: TimelineStore | None, *, admin_token: str | None
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/screen-awareness",
        tags=["admin-screen-awareness"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    @router.get("/status", response_model=ScreenAwarenessStatus)
    async def status(request: Request) -> ScreenAwarenessStatus:
        loop = _loop_from(request)
        if loop is None:
            return ScreenAwarenessStatus(
                configured_enabled=False,
                interval_seconds=60,
                displays=[],
                memory_enabled=False,
                proactive_enabled=False,
                loop_running=False,
                cycles=0,
                last_tick_at=None,
                last_error="screen awareness not assembled (requires database config)",
                display_states=[],
            )
        config = loop._config_store.current.config.screen_awareness
        state = loop.state
        display_states = [
            ScreenAwarenessDisplayState(
                display=display,
                last_hash=f"{ds.last_hash:016x}" if ds.last_hash is not None else None,
                last_analyzed_at=ds.last_analyzed_at,
                last_summary=ds.last_summary,
                consecutive_failures=ds.consecutive_failures,
                cooldown_until=ds.disabled_until,
            )
            for display, ds in sorted(state.displays.items())
        ]
        return ScreenAwarenessStatus(
            configured_enabled=config.enabled,
            interval_seconds=config.interval_seconds,
            displays=list(config.displays),
            memory_enabled=config.memory_enabled,
            proactive_enabled=config.proactive_enabled,
            loop_running=state.running,
            cycles=state.cycles,
            last_tick_at=state.last_tick_at,
            last_error=state.last_error,
            display_states=display_states,
        )

    @router.get("/observations", response_model=ScreenObservationsResponse)
    async def observations(
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ScreenObservationsResponse:
        if timeline is None:
            return ScreenObservationsResponse(items=[], total=0, limit=limit, offset=offset)
        async with timeline.database.sessions() as session:
            from sqlalchemy import select

            from app.db import AppUserRecord

            owner = await session.scalar(
                select(AppUserRecord.id).order_by(AppUserRecord.created_at)
            )
        if owner is None:
            return ScreenObservationsResponse(items=[], total=0, limit=limit, offset=offset)
        filters = {
            "source_types": (TimelineSourceType.DEVICE,),
            "event_types": ("screen.observed",),
            "privacy_levels": (PrivacyLevel.L0, PrivacyLevel.L1),
        }
        total = await timeline.count_events(user_id=UUID(str(owner)), **filters)
        result = await timeline.search(
            user_id=UUID(str(owner)), **filters, limit=limit, offset=offset
        )
        items = []
        for event in result.events:
            raw_display = (event.metadata or {}).get("display", 0)
            display = int(raw_display) if isinstance(raw_display, (int, float, str)) else 0
            items.append(
                ScreenObservationItem(
                    id=event.id,
                    occurred_at=event.occurred_at,
                    display=display,
                    title=event.title or "屏幕观察",
                    summary=event.summary,
                    importance=event.importance,
                    metadata=dict(event.metadata or {}),
                )
            )
        return ScreenObservationsResponse(items=items, total=total, limit=limit, offset=offset)

    return router
