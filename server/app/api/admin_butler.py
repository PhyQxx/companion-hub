"""个人管家管理：列表、模板创建、会议文本整理与显式行动项确认。

写操作复用领域校验和用户范围；模板保存不执行动作，会议文本需显式授权。
会话访问限本人，机器 token 缺省取第一个活跃用户。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select

from app.db import AppUserRecord, Database
from app.home_scene.models import HomeSceneStep, HomeSceneView
from app.home_scene.service import HomeSceneService
from app.meetings.models import MeetingView
from app.meetings.service import MeetingService
from app.schemas.common import PrivacyLevel, StrictModel
from app.schemas.evaluation import WorkflowFixtureRequest
from app.tasks.activity_summary import ActivitySummaryService, ActivitySummaryView, Kind
from app.tasks.brief import BriefView, DailyBriefService
from app.tasks.review import DailyReviewService, ReviewView
from app.workflows.drafts import PlanDistiller, WorkflowDraftStore, replay_pending_draft
from app.workflows.models import WorkflowStep, WorkflowView
from app.workflows.service import WorkflowService

from .admin_config import AdminTokenGuard, admin_access
from .home_scenes import HomeScenePayload
from .meetings import (
    ConfirmMeetingActionRequest,
    PrepareMeetingRequest,
    TranscriptAppendRequest,
    TranscriptionConsentRequest,
)
from .workflows import WorkflowPayload


class AdminButlerSummary(StrictModel):
    user_id: UUID
    workflows: int
    scenes: int
    active_scenes: int
    meetings: int
    briefs: int
    reviews: int
    latest_brief_date: date | None = None
    latest_review_date: date | None = None


class WorkflowDraftAdminView(StrictModel):
    """DIST 流程草稿的 Admin 视图：含回放证据，不含步骤执行参数。"""

    id: UUID
    user_id: UUID
    plan_id: UUID | None
    name: str
    description: str | None
    steps: list[WorkflowStep]
    status: str
    replay_status: str
    replay_detail: dict[str, object]
    created_at: datetime
    reviewed_at: datetime | None


def _draft_view(record: Any) -> WorkflowDraftAdminView:
    return WorkflowDraftAdminView(
        id=record.id,
        user_id=record.user_id,
        plan_id=record.plan_id,
        name=record.name,
        description=record.description,
        steps=[WorkflowStep.model_validate(item) for item in record.steps or []],
        status=record.status,
        replay_status=record.replay_status,
        replay_detail=dict(record.replay_detail or {}),
        created_at=record.created_at,
        reviewed_at=record.reviewed_at,
    )


def create_admin_butler_router(
    *,
    database: Database,
    workflows: WorkflowService,
    scenes: HomeSceneService,
    meetings: MeetingService,
    briefs: DailyBriefService,
    reviews: DailyReviewService,
    admin_token: str | None,
    drafts: WorkflowDraftStore | None = None,
    distiller: PlanDistiller | None = None,
) -> APIRouter:
    guard = AdminTokenGuard(admin_token, min_role="member")
    router = APIRouter(
        prefix="/api/v1/admin/butler",
        tags=["admin-butler"],
        dependencies=[Depends(guard)],
    )

    async def resolve_user(request: Request, user_id: UUID | None = None) -> UUID:
        scoped = admin_access(request).scoped_user_id(user_id)
        if scoped is not None:
            return scoped
        async with database.sessions() as session:
            first = await session.scalar(
                select(AppUserRecord.id)
                .where(AppUserRecord.status == "active")
                .order_by(AppUserRecord.created_at)
                .limit(1)
            )
        if first is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no active user")
        return first

    @router.get("/actions")
    async def available_actions() -> list[dict[str, object]]:
        return [
            item.model_dump(mode="json")
            for item in workflows.registry.definitions()
            if item.risk != "A3"
        ]

    @router.post("/workflows/preview")
    async def preview_workflow(body: WorkflowPayload) -> object:
        try:
            return await workflows.preview(
                name=body.name, description=body.description, steps=body.steps
            )
        except (ValueError, LookupError, PermissionError) as error:
            raise HTTPException(422, str(error)) from error

    @router.post("/workflows", status_code=201)
    async def create_workflow(body: WorkflowPayload, request: Request) -> WorkflowView:
        try:
            return await workflows.save_workflow(
                user_id=await resolve_user(request),
                name=body.name,
                description=body.description,
                steps=body.steps,
            )
        except (ValueError, LookupError, PermissionError) as error:
            raise HTTPException(422, str(error)) from error

    @router.post("/scenes", status_code=201)
    async def create_scene(body: HomeScenePayload, request: Request) -> HomeSceneView:
        try:
            return await scenes.create_scene(
                user_id=await resolve_user(request),
                name=body.name,
                trigger=body.trigger,
                steps=[HomeSceneStep.model_validate(step.model_dump()) for step in body.steps],
                window_start=body.window_start,
                window_end=body.window_end,
            )
        except (ValueError, LookupError, PermissionError) as error:
            raise HTTPException(422, str(error)) from error

    @router.post("/meetings", status_code=201)
    async def create_meeting(body: PrepareMeetingRequest, request: Request) -> MeetingView:
        try:
            return await meetings.prepare(
                await resolve_user(request),
                title=body.title,
                participants=body.participants,
                privacy_level=PrivacyLevel(body.privacy_level),
                calendar_event_id=body.calendar_event_id,
            )
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @router.post("/meetings/{meeting_id}/transcription/authorize")
    async def authorize_meeting(
        meeting_id: UUID, body: TranscriptionConsentRequest, request: Request
    ) -> MeetingView:
        try:
            return await meetings.authorize_transcription(await resolve_user(request), meeting_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/meetings/{meeting_id}/transcription/revoke")
    async def revoke_meeting(meeting_id: UUID, request: Request) -> MeetingView:
        try:
            return await meetings.revoke_transcription(await resolve_user(request), meeting_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/meetings/{meeting_id}/transcript")
    async def append_meeting(
        meeting_id: UUID, body: TranscriptAppendRequest, request: Request
    ) -> MeetingView:
        try:
            return await meetings.append_transcript(
                await resolve_user(request), meeting_id, body.segments
            )
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @router.post("/meetings/{meeting_id}/finish")
    async def finish_meeting(meeting_id: UUID, request: Request) -> MeetingView:
        try:
            return await meetings.finish(await resolve_user(request), meeting_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/meetings/{meeting_id}/actions/{index}/confirm")
    async def confirm_meeting_action(
        meeting_id: UUID, index: int, body: ConfirmMeetingActionRequest, request: Request
    ) -> MeetingView:
        try:
            return await meetings.confirm_action_item(
                await resolve_user(request), meeting_id, index, due_at=body.due_at
            )
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @router.get("/summary", response_model=AdminButlerSummary)
    async def summary(request: Request, user_id: UUID | None = None) -> AdminButlerSummary:
        owner = await resolve_user(request, user_id)
        workflow_items = await workflows.list_workflows(owner, limit=100)
        scene_items = await scenes.list_scenes(owner)
        meeting_items = await meetings.list(owner, limit=100)
        brief_items = await briefs.recent_briefs(owner, limit=30)
        review_items = await reviews.recent_reviews(owner, limit=30)
        return AdminButlerSummary(
            user_id=owner,
            workflows=len(workflow_items),
            scenes=len(scene_items),
            active_scenes=sum(1 for scene in scene_items if scene.enabled),
            meetings=len(meeting_items),
            briefs=len(brief_items),
            reviews=len(review_items),
            latest_brief_date=brief_items[0].brief_date if brief_items else None,
            latest_review_date=review_items[0].review_date if review_items else None,
        )

    @router.get("/workflows", response_model=list[WorkflowView])
    async def list_workflows(
        request: Request,
        user_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[WorkflowView]:
        return await workflows.list_workflows(await resolve_user(request, user_id), limit=limit)

    @router.delete("/workflows/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_workflow(
        workflow_id: UUID, request: Request, user_id: UUID | None = None
    ) -> None:
        try:
            await workflows.delete_workflow(await resolve_user(request, user_id), workflow_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @router.get("/workflow-drafts", response_model=list[WorkflowDraftAdminView])
    async def list_workflow_drafts(
        request: Request,
        user_id: UUID | None = None,
        draft_status: Annotated[str | None, Query(alias="status")] = "pending",
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[WorkflowDraftAdminView]:
        if drafts is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="drafts_disabled")
        owner = await resolve_user(request, user_id)
        records = await drafts.list_drafts(owner, status=draft_status, limit=limit)
        return [_draft_view(record) for record in records]

    @router.post("/workflow-drafts/{draft_id}/approve", response_model=WorkflowDraftAdminView)
    async def approve_workflow_draft(
        draft_id: UUID, request: Request, user_id: UUID | None = None
    ) -> WorkflowDraftAdminView:
        """审批通过：回放通过（或无可回放步骤）才允许创建正式流程。"""
        if drafts is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="drafts_disabled")
        try:
            record = await drafts.approve(
                draft_id, user_id=await resolve_user(request, user_id), workflows=workflows
            )
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return _draft_view(record)

    @router.post("/workflow-drafts/{draft_id}/evaluate", response_model=WorkflowDraftAdminView)
    async def evaluate_workflow_draft(
        draft_id: UUID,
        body: WorkflowFixtureRequest,
        request: Request,
        user_id: UUID | None = None,
    ) -> WorkflowDraftAdminView:
        if drafts is None:
            raise HTTPException(503, "drafts_disabled")
        try:
            record = await drafts.evaluate_draft(
                draft_id,
                user_id=await resolve_user(request, user_id),
                registry=workflows.registry,
                corpus=body,
            )
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return _draft_view(record)

    @router.post("/workflow-drafts/{draft_id}/dismiss", response_model=WorkflowDraftAdminView)
    async def dismiss_workflow_draft(
        draft_id: UUID, request: Request, user_id: UUID | None = None
    ) -> WorkflowDraftAdminView:
        if drafts is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="drafts_disabled")
        record = await drafts.get(draft_id)
        if record is None or record.status != "pending":
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="draft not found")
        if record.user_id != await resolve_user(request, user_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="draft not found")
        reviewed = await drafts.mark_reviewed(draft_id, status="dismissed")
        assert reviewed is not None
        return _draft_view(reviewed)

    @router.post("/workflow-drafts/{draft_id}/replay", response_model=WorkflowDraftAdminView)
    async def replay_workflow_draft(
        draft_id: UUID, request: Request, user_id: UUID | None = None
    ) -> WorkflowDraftAdminView:
        """手动重跑样例回放；结果写回草稿作为审批依据。"""
        if drafts is None or distiller is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="drafts_disabled")
        record = await replay_pending_draft(
            distiller, drafts, draft_id, user_id=await resolve_user(request, user_id)
        )
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="draft not found")
        return _draft_view(record)

    @router.get("/scenes", response_model=list[HomeSceneView])
    async def list_scenes(request: Request, user_id: UUID | None = None) -> list[HomeSceneView]:
        return await scenes.list_scenes(await resolve_user(request, user_id))

    @router.post("/scenes/{scene_id}/enable", response_model=HomeSceneView)
    async def enable_scene(
        scene_id: UUID, request: Request, user_id: UUID | None = None
    ) -> HomeSceneView:
        try:
            owner = await resolve_user(request, user_id)
            return await scenes.set_enabled(owner, scene_id, enabled=True)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @router.post("/scenes/{scene_id}/disable", response_model=HomeSceneView)
    async def disable_scene(
        scene_id: UUID, request: Request, user_id: UUID | None = None
    ) -> HomeSceneView:
        try:
            owner = await resolve_user(request, user_id)
            return await scenes.set_enabled(owner, scene_id, enabled=False)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @router.delete("/scenes/{scene_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_scene(scene_id: UUID, request: Request, user_id: UUID | None = None) -> None:
        try:
            await scenes.delete_scene(await resolve_user(request, user_id), scene_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @router.get("/meetings", response_model=list[MeetingView])
    async def list_meetings(
        request: Request,
        user_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[MeetingView]:
        return await meetings.list(await resolve_user(request, user_id), limit=limit)

    @router.get("/briefs", response_model=list[BriefView])
    async def list_briefs(
        request: Request,
        user_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=30)] = 14,
    ) -> list[BriefView]:
        return await briefs.recent_briefs(await resolve_user(request, user_id), limit=limit)

    @router.get("/activity-summaries", response_model=list[ActivitySummaryView])
    async def list_activity_summaries(
        request: Request,
        kind: Kind,
        user_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[ActivitySummaryView]:
        return await ActivitySummaryService(database).recent(
            await resolve_user(request, user_id), kind=kind, limit=limit, offset=offset
        )

    @router.get("/reviews", response_model=list[ReviewView])
    async def list_reviews(
        request: Request,
        user_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=30)] = 14,
    ) -> list[ReviewView]:
        return await reviews.recent_reviews(await resolve_user(request, user_id), limit=limit)

    return router
