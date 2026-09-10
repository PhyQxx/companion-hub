"""MEET-01 用户会议 API：准备、授权转写、整理与行动项确认。"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field

from app.auth import AuthService, ChatPrincipal
from app.meetings import MeetingService, MeetingView, TranscriptSegment
from app.schemas import PrivacyLevel
from app.schemas.common import StrictModel

from .auth import ChatSessionGuard


class PrepareMeetingRequest(StrictModel):
    title: Annotated[str, Field(min_length=1, max_length=320)] | None = None
    calendar_event_id: UUID | None = None
    participants: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=120)]],
        Field(max_length=100),
    ] = Field(default_factory=list)
    privacy_level: Literal["L1", "L2"] = "L1"


class TranscriptionConsentRequest(StrictModel):
    authorized: Literal[True]


class TranscriptAppendRequest(StrictModel):
    segments: Annotated[list[TranscriptSegment], Field(min_length=1, max_length=50)]


class ConfirmMeetingActionRequest(StrictModel):
    due_at: datetime | None = None


def create_meetings_router(service: MeetingService, auth_service: AuthService) -> APIRouter:
    guard = ChatSessionGuard(auth_service)
    router = APIRouter(prefix="/api/v1/meetings", tags=["meetings"])

    @router.post("", response_model=MeetingView, status_code=status.HTTP_201_CREATED)
    async def prepare_meeting(
        body: PrepareMeetingRequest,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> MeetingView:
        try:
            return await service.prepare(
                principal.user_id,
                title=body.title,
                participants=body.participants,
                privacy_level=PrivacyLevel(body.privacy_level),
                calendar_event_id=body.calendar_event_id,
            )
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    @router.get("", response_model=list[MeetingView])
    async def list_meetings(
        principal: Annotated[ChatPrincipal, Depends(guard)],
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ) -> list[MeetingView]:
        return await service.list(principal.user_id, limit=limit)

    @router.get("/{meeting_id}", response_model=MeetingView)
    async def get_meeting(
        meeting_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> MeetingView:
        try:
            return await service.get(principal.user_id, meeting_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @router.post("/{meeting_id}/transcription/authorize", response_model=MeetingView)
    async def authorize_transcription(
        meeting_id: UUID,
        body: TranscriptionConsentRequest,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> MeetingView:
        del body
        try:
            return await service.authorize_transcription(principal.user_id, meeting_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error

    @router.post("/{meeting_id}/transcription/revoke", response_model=MeetingView)
    async def revoke_transcription(
        meeting_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> MeetingView:
        try:
            return await service.revoke_transcription(principal.user_id, meeting_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error

    @router.post("/{meeting_id}/transcript", response_model=MeetingView)
    async def append_transcript(
        meeting_id: UUID,
        body: TranscriptAppendRequest,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> MeetingView:
        try:
            return await service.append_transcript(
                principal.user_id, meeting_id, body.segments
            )
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    @router.post("/{meeting_id}/finish", response_model=MeetingView)
    async def finish_meeting(
        meeting_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> MeetingView:
        try:
            return await service.finish(principal.user_id, meeting_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error

    @router.post("/{meeting_id}/cancel", response_model=MeetingView)
    async def cancel_meeting(
        meeting_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> MeetingView:
        try:
            return await service.cancel(principal.user_id, meeting_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error

    @router.post("/{meeting_id}/actions/{index}/confirm", response_model=MeetingView)
    async def confirm_action_item(
        meeting_id: UUID,
        index: int,
        body: ConfirmMeetingActionRequest,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> MeetingView:
        try:
            return await service.confirm_action_item(
                principal.user_id, meeting_id, index, due_at=body.due_at
            )
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error

    return router
