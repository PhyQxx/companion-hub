from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field

from app.auth import AuthService, ChatPrincipal
from app.cognition import (
    CognitiveDecisionView,
    CognitiveStore,
    FeedbackKind,
    GoalKind,
    GoalStatus,
    GoalView,
    ReflectionCandidate,
)
from app.db import CognitiveDecisionRecord
from app.perception import PerceptionStore, SemanticEventAuditView
from app.schemas.common import StrictModel

from .auth import ChatSessionGuard


class CreateGoalRequest(StrictModel):
    kind: Literal["user", "shared"]
    title: Annotated[str, Field(min_length=1, max_length=320)]
    source_kind: Literal["message", "manual"]
    source_id: Annotated[str, Field(min_length=1, max_length=200)]
    due_at: datetime | None = None
    expires_at: datetime | None = None


class FeedbackRequest(StrictModel):
    kind: Literal["accepted", "ignored", "snoozed", "forbidden"]


class UpdateGoalRequest(StrictModel):
    status: Literal["completed", "cancelled"]


class FeedbackResponse(StrictModel):
    id: UUID
    reflection_candidate: ReflectionCandidate | None = None


def create_cognition_router(
    store: CognitiveStore,
    auth_service: AuthService,
    perception_store: PerceptionStore | None = None,
) -> APIRouter:
    guard = ChatSessionGuard(auth_service)
    router = APIRouter(prefix="/api/v1/cognition", tags=["cognition"])

    @router.post("/goals", response_model=GoalView, status_code=status.HTTP_201_CREATED)
    async def create_goal(
        body: CreateGoalRequest,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> GoalView:
        try:
            return await store.create_goal(
                user_id=principal.user_id,
                kind=GoalKind(body.kind),
                title=body.title,
                source_kind=body.source_kind,
                source_id=body.source_id,
                due_at=body.due_at,
                expires_at=body.expires_at,
            )
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    @router.get("/decisions", response_model=list[CognitiveDecisionView])
    async def list_decisions(
        principal: Annotated[ChatPrincipal, Depends(guard)],
        limit: Annotated[int, Field(ge=1, le=200)] = 100,
    ) -> list[CognitiveDecisionView]:
        return await store.recent_decisions(principal.user_id, limit=limit)

    @router.get("/goals", response_model=list[GoalView])
    async def list_active_goals(
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> list[GoalView]:
        return await store.active_goals(principal.user_id, now=datetime.now(UTC))

    @router.patch("/goals/{goal_id}", response_model=GoalView)
    async def update_goal(
        goal_id: UUID,
        body: UpdateGoalRequest,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> GoalView:
        try:
            return await store.set_goal_status(
                user_id=principal.user_id,
                goal_id=goal_id,
                status=GoalStatus(body.status),
            )
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error

    @router.post("/decisions/{decision_id}/feedback", response_model=FeedbackResponse)
    async def add_feedback(
        decision_id: UUID,
        body: FeedbackRequest,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> FeedbackResponse:
        try:
            feedback_id = await store.add_feedback(
                user_id=principal.user_id,
                decision_id=decision_id,
                kind=FeedbackKind(body.kind),
            )
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        async with store.database.sessions() as session:
            decision = await session.get(CognitiveDecisionRecord, decision_id)
        candidate = (
            await store.reflection_candidate(
                user_id=principal.user_id,
                trigger_kind=decision.trigger_kind,
            )
            if decision is not None
            else None
        )
        return FeedbackResponse(id=feedback_id, reflection_candidate=candidate)

    if perception_store is not None:

        @router.get("/perception-events", response_model=list[SemanticEventAuditView])
        async def list_perception_events(
            principal: Annotated[ChatPrincipal, Depends(guard)],
            limit: Annotated[int, Field(ge=1, le=200)] = 100,
        ) -> list[SemanticEventAuditView]:
            return await perception_store.recent(principal.user_id, limit=limit)

    return router
