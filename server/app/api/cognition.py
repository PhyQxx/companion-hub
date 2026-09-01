from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field

from app.auth import AuthService, ChatPrincipal
from app.cognition import (
    ActionDefinition,
    ActionInvocation,
    ActionPlanService,
    ActionPlanView,
    ActionRegistry,
    ActionResult,
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


class GoalReminderFeedbackRequest(StrictModel):
    kind: Literal["ignored", "snoozed"]
    minutes: Annotated[int, Field(ge=1, le=10_080)] | None = None


class CreateActionPlanRequest(StrictModel):
    title: Annotated[str, Field(min_length=1, max_length=240)] | None = None
    steps: Annotated[list[ActionInvocation], Field(min_length=1, max_length=10)]
    ttl_seconds: Annotated[int, Field(ge=30, le=3_600)] = 300
    idempotency_key: Annotated[str, Field(min_length=8, max_length=160)] | None = None


class FeedbackResponse(StrictModel):
    id: UUID
    reflection_candidate: ReflectionCandidate | None = None


def create_cognition_router(
    store: CognitiveStore,
    auth_service: AuthService,
    perception_store: PerceptionStore | None = None,
    action_registry: ActionRegistry | None = None,
    action_plan_service: ActionPlanService | None = None,
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

    @router.post("/goals/{goal_id}/reminder-feedback", response_model=GoalView)
    async def goal_reminder_feedback(
        goal_id: UUID,
        body: GoalReminderFeedbackRequest,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> GoalView:
        """GOAL-01 忽略降频 / 稍后：忽略顺延一天并计数，稍后推迟到指定时间。"""
        try:
            if body.kind == "ignored":
                return await store.ignore_goal_reminder(
                    user_id=principal.user_id,
                    goal_id=goal_id,
                )
            until = datetime.now(UTC) + timedelta(minutes=body.minutes or 60)
            return await store.defer_goal_reminders(
                user_id=principal.user_id,
                goal_id=goal_id,
                until=until,
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

    @router.get("/action-results", response_model=list[ActionResult])
    async def list_action_results(
        principal: Annotated[ChatPrincipal, Depends(guard)],
        limit: Annotated[int, Field(ge=1, le=200)] = 100,
    ) -> list[ActionResult]:
        return await store.recent_action_results(principal.user_id, limit=limit)

    if action_registry is not None:

        @router.get("/actions/catalog", response_model=list[ActionDefinition])
        async def list_action_catalog(
            _: Annotated[ChatPrincipal, Depends(guard)],
        ) -> list[ActionDefinition]:
            return action_registry.definitions()

    if action_plan_service is not None:

        @router.post(
            "/action-plans",
            response_model=ActionPlanView,
            status_code=status.HTTP_201_CREATED,
        )
        async def create_action_plan(
            body: CreateActionPlanRequest,
            principal: Annotated[ChatPrincipal, Depends(guard)],
        ) -> ActionPlanView:
            try:
                return await action_plan_service.create_plan(
                    user_id=principal.user_id,
                    invocations=body.steps,
                    title=body.title,
                    ttl_seconds=body.ttl_seconds,
                    idempotency_key=body.idempotency_key,
                )
            except LookupError as error:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
            except (PermissionError, ValueError) as error:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(error),
                ) from error

        @router.get("/action-plans/{plan_id}", response_model=ActionPlanView)
        async def get_action_plan(
            plan_id: UUID,
            principal: Annotated[ChatPrincipal, Depends(guard)],
        ) -> ActionPlanView:
            try:
                return await action_plan_service.get_plan(
                    user_id=principal.user_id,
                    plan_id=plan_id,
                )
            except LookupError as error:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

        @router.post("/action-plans/{plan_id}/confirm", response_model=ActionPlanView)
        async def confirm_action_plan(
            plan_id: UUID,
            principal: Annotated[ChatPrincipal, Depends(guard)],
        ) -> ActionPlanView:
            try:
                return await action_plan_service.confirm_plan(
                    user_id=principal.user_id,
                    plan_id=plan_id,
                )
            except LookupError as error:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
            except ValueError as error:
                raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error

        @router.post("/action-plans/{plan_id}/cancel", response_model=ActionPlanView)
        async def cancel_action_plan(
            plan_id: UUID,
            principal: Annotated[ChatPrincipal, Depends(guard)],
        ) -> ActionPlanView:
            try:
                return await action_plan_service.cancel_plan(
                    user_id=principal.user_id,
                    plan_id=plan_id,
                )
            except LookupError as error:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
            except ValueError as error:
                raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error

        @router.post("/action-plans/{plan_id}/execute", response_model=ActionPlanView)
        async def execute_action_plan(
            plan_id: UUID,
            principal: Annotated[ChatPrincipal, Depends(guard)],
        ) -> ActionPlanView:
            try:
                return await action_plan_service.execute_plan(
                    user_id=principal.user_id,
                    plan_id=plan_id,
                )
            except LookupError as error:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
            except RuntimeError as error:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=str(error),
                ) from error
            except ValueError as error:
                raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error

        @router.post(
            "/action-plans/{plan_id}/undo",
            response_model=ActionPlanView,
            status_code=status.HTTP_201_CREATED,
        )
        async def create_action_undo_plan(
            plan_id: UUID,
            principal: Annotated[ChatPrincipal, Depends(guard)],
        ) -> ActionPlanView:
            try:
                return await action_plan_service.create_undo_plan(
                    user_id=principal.user_id,
                    plan_id=plan_id,
                )
            except LookupError as error:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
            except ValueError as error:
                raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error

    @router.get("/reflection-candidates", response_model=list[ReflectionCandidate])
    async def list_reflection_candidates(
        principal: Annotated[ChatPrincipal, Depends(guard)],
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> list[ReflectionCandidate]:
        return await store.pending_candidates(principal.user_id, limit=limit)

    if perception_store is not None:

        @router.get("/perception-events", response_model=list[SemanticEventAuditView])
        async def list_perception_events(
            principal: Annotated[ChatPrincipal, Depends(guard)],
            limit: Annotated[int, Field(ge=1, le=200)] = 100,
        ) -> list[SemanticEventAuditView]:
            return await perception_store.recent(principal.user_id, limit=limit)

    return router
