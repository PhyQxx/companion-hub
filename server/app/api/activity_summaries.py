"""Authenticated history, explicit generation and removal of reports."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.auth import AuthService, ChatPrincipal
from app.tasks.activity_summary import ActivitySummaryService, ActivitySummaryView, Kind

from .auth import ChatSessionGuard


def create_activity_summaries_router(
    service: ActivitySummaryService, auth_service: AuthService
) -> APIRouter:
    guard = ChatSessionGuard(auth_service)
    router = APIRouter(prefix="/api/v1/activity-summaries", tags=["activity-summaries"])

    @router.get("", response_model=list[ActivitySummaryView])
    async def history(
        principal: Annotated[ChatPrincipal, Depends(guard)],
        kind: Kind | None = None,
        limit: int = Query(30, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ) -> list[ActivitySummaryView]:
        return await service.recent(principal.user_id, kind=kind, limit=limit, offset=offset)

    @router.post("/generate", response_model=ActivitySummaryView)
    async def generate(
        principal: Annotated[ChatPrincipal, Depends(guard)], kind: Kind = "daily"
    ) -> ActivitySummaryView:
        return await service.build(principal.user_id, kind)

    @router.delete("/{summary_id}", status_code=204)
    async def remove(summary_id: UUID, principal: Annotated[ChatPrincipal, Depends(guard)]) -> None:
        await service.remove(principal.user_id, summary_id)

    return router
