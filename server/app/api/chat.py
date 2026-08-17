from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field

from app.auth import AuthService, ChatPrincipal
from app.chat import ChatService, ChatTurn, ConversationView, MessageView
from app.llm import LLMRouteExhausted
from app.privacy import EgressBlocked
from app.schemas import PrivacyLevel
from app.schemas.common import StrictModel

from .auth import ChatSessionGuard


class CreateConversationRequest(StrictModel):
    title: Annotated[str, Field(min_length=1, max_length=240)] | None = None


class SendMessageRequest(StrictModel):
    text: Annotated[str, Field(min_length=1, max_length=20_000)]
    privacy_level: Literal["L0", "L1", "L2"] = "L1"


class ConversationResponse(StrictModel):
    id: UUID
    user_id: UUID
    title: str | None
    status: str
    last_seq: int
    created_at: datetime
    last_active_at: datetime


class MessageResponse(StrictModel):
    id: UUID
    conversation_id: UUID
    turn_id: UUID
    seq: int
    role: str
    content: str
    privacy_level: str
    generation_id: UUID | None
    decision_meta: dict[str, object] | None
    created_at: datetime


class ChatTurnResponse(StrictModel):
    user_message: MessageResponse
    assistant_message: MessageResponse


def create_chat_router(service: ChatService, auth_service: AuthService) -> APIRouter:
    chat_guard = ChatSessionGuard(auth_service)
    router = APIRouter(
        prefix="/api/v1/chat",
        tags=["chat"],
    )

    @router.post(
        "/conversations",
        response_model=ConversationResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_conversation(
        body: CreateConversationRequest,
        principal: Annotated[ChatPrincipal, Depends(chat_guard)],
    ) -> ConversationResponse:
        try:
            result = await service.create_conversation(
                user_id=principal.user_id,
                title=body.title,
            )
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        return _conversation_response(result)

    @router.get("/conversations", response_model=list[ConversationResponse])
    async def list_conversations(
        principal: Annotated[ChatPrincipal, Depends(chat_guard)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[ConversationResponse]:
        return [
            _conversation_response(item)
            for item in await service.list_conversations(
                user_id=principal.user_id, limit=limit
            )
        ]

    @router.get(
        "/conversations/{conversation_id}/messages",
        response_model=list[MessageResponse],
    )
    async def list_messages(
        conversation_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(chat_guard)],
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[MessageResponse]:
        try:
            result = await service.list_messages(
                conversation_id, user_id=principal.user_id, limit=limit
            )
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        return [_message_response(item) for item in result]

    @router.post(
        "/conversations/{conversation_id}/messages",
        response_model=ChatTurnResponse,
    )
    async def send_message(
        conversation_id: UUID,
        body: SendMessageRequest,
        principal: Annotated[ChatPrincipal, Depends(chat_guard)],
    ) -> ChatTurnResponse:
        try:
            result = await service.send_message(
                conversation_id,
                user_id=principal.user_id,
                text=body.text,
                privacy_level=PrivacyLevel(body.privacy_level),
            )
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        except (EgressBlocked, LLMRouteExhausted) as error:
            reason_code = getattr(error, "reason_code", str(error))
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"reason_code": reason_code},
            ) from error
        except Exception as error:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"reason_code": "model_completion_unavailable"},
            ) from error
        return _turn_response(result)

    return router


def _conversation_response(value: ConversationView) -> ConversationResponse:
    return ConversationResponse(
        id=value.id,
        user_id=value.user_id,
        title=value.title,
        status=value.status,
        last_seq=value.last_seq,
        created_at=value.created_at,
        last_active_at=value.last_active_at,
    )


def _message_response(value: MessageView) -> MessageResponse:
    return MessageResponse(
        id=value.id,
        conversation_id=value.conversation_id,
        turn_id=value.turn_id,
        seq=value.seq,
        role=value.role,
        content=value.content,
        privacy_level=value.privacy_level,
        generation_id=value.generation_id,
        decision_meta=value.decision_meta,
        created_at=value.created_at,
    )


def _turn_response(value: ChatTurn) -> ChatTurnResponse:
    return ChatTurnResponse(
        user_message=_message_response(value.user_message),
        assistant_message=_message_response(value.assistant_message),
    )
