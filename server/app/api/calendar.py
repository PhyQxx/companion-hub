"""CAL-01 用户日历 API：预览/创建/查询/改期/取消。

注意：不要加 `from __future__ import annotations`——FastAPI 0.141 的懒路由
在延迟求值注解时无法解析 `Depends(guard)`，会把 principal 降级成 query 参数。
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field

from app.auth import AuthService, ChatPrincipal
from app.calendar import (
    CalendarCreateTool,
    CalendarEventView,
    CalendarParticipant,
    CalendarPreview,
    CalendarService,
)
from app.schemas.common import StrictModel

from .auth import ChatSessionGuard


class CalendarEventPayload(StrictModel):
    title: Annotated[str, Field(min_length=1, max_length=320)]
    starts_at: datetime
    ends_at: datetime
    calendar_id: Annotated[str, Field(min_length=1, max_length=64)] = "primary"
    notes: Annotated[str, Field(max_length=2000)] | None = None
    location: Annotated[str, Field(max_length=240)] | None = None
    participants: Annotated[list[CalendarParticipant], Field(max_length=20)] = Field(
        default_factory=list
    )
    reminder_lead_minutes: Annotated[int, Field(ge=0, le=1440)] = 10


class CalendarPatchPayload(StrictModel):
    title: Annotated[str, Field(min_length=1, max_length=320)] | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    location: Annotated[str, Field(max_length=240)] | None = None
    notes: Annotated[str, Field(max_length=2000)] | None = None
    reminder_lead_minutes: Annotated[int, Field(ge=0, le=1440)] | None = None


class CalendarDraftConfirmation(StrictModel):
    digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def create_calendar_router(
    service: CalendarService,
    auth_service: AuthService,
    create_tool: CalendarCreateTool | None = None,
) -> APIRouter:
    guard = ChatSessionGuard(auth_service)
    router = APIRouter(prefix="/api/v1/calendar", tags=["calendar"])

    if create_tool is not None:

        @router.get("/drafts")
        async def list_drafts(
            principal: Annotated[ChatPrincipal, Depends(guard)],
        ) -> list[dict[str, object]]:
            return create_tool.list_drafts(principal.user_id)

        @router.post("/drafts/{draft_id}/confirm")
        async def confirm_draft(
            draft_id: UUID,
            body: CalendarDraftConfirmation,
            principal: Annotated[ChatPrincipal, Depends(guard)],
        ) -> dict[str, object]:
            try:
                return await create_tool.confirm(principal.user_id, draft_id, body.digest)
            except LookupError as error:
                raise HTTPException(404, str(error)) from error
            except ValueError as error:
                raise HTTPException(409, str(error)) from error

        @router.post("/drafts/{draft_id}/cancel")
        async def cancel_draft(
            draft_id: UUID,
            principal: Annotated[ChatPrincipal, Depends(guard)],
        ) -> dict[str, object]:
            try:
                return create_tool.cancel(principal.user_id, draft_id)
            except LookupError as error:
                raise HTTPException(404, str(error)) from error
            except ValueError as error:
                raise HTTPException(409, str(error)) from error

    @router.post("/events/preview", response_model=CalendarPreview)
    async def preview_event(
        body: CalendarEventPayload,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> CalendarPreview:
        """写入前预览：展示时间、参与人、目标日历与冲突，不落库。"""
        try:
            return await service.preview(
                principal.user_id,
                title=body.title,
                starts_at=body.starts_at,
                ends_at=body.ends_at,
                calendar_id=body.calendar_id,
                location=body.location,
                participants=body.participants,
                reminder_lead_minutes=body.reminder_lead_minutes,
            )
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    @router.post("/events", response_model=CalendarEventView, status_code=status.HTTP_201_CREATED)
    async def create_event(
        body: CalendarEventPayload,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> CalendarEventView:
        try:
            return await service.create_event(
                principal.user_id,
                title=body.title,
                starts_at=body.starts_at,
                ends_at=body.ends_at,
                calendar_id=body.calendar_id,
                notes=body.notes,
                location=body.location,
                participants=body.participants,
                reminder_lead_minutes=body.reminder_lead_minutes,
            )
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    @router.get("/events", response_model=list[CalendarEventView])
    async def list_events(
        principal: Annotated[ChatPrincipal, Depends(guard)],
        starts_from: Annotated[datetime | None, Query()] = None,
        starts_to: Annotated[datetime | None, Query()] = None,
    ) -> list[CalendarEventView]:
        return await service.list_events(
            principal.user_id, starts_from=starts_from, starts_to=starts_to
        )

    @router.patch("/events/{event_id}", response_model=CalendarEventView)
    async def patch_event(
        event_id: UUID,
        body: CalendarPatchPayload,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> CalendarEventView:
        try:
            return await service.reschedule_event(
                principal.user_id,
                event_id,
                title=body.title,
                starts_at=body.starts_at,
                ends_at=body.ends_at,
                location=body.location,
                notes=body.notes,
                reminder_lead_minutes=body.reminder_lead_minutes,
            )
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    @router.post("/events/{event_id}/cancel", response_model=CalendarEventView)
    async def cancel_event(
        event_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> CalendarEventView:
        try:
            return await service.cancel_event(principal.user_id, event_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error

    return router
