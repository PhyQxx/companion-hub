"""管家能力 Admin 可视化：流程 / 家庭场景 / 会议 / 简报 / 回顾。

只读为主，用于运维检视与验收支撑；开放的写操作仅限删除流程模板与
场景启停这类无设备副作用的动作——运行、计划确认、会议授权与摘要
确认仍必须走聊天端确认流，Admin 不提供替代入口。

单用户部署（ID-01 暂缓）：user_id 缺省取第一个活跃用户。
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.db import AppUserRecord, Database
from app.home_scene.models import HomeSceneView
from app.home_scene.service import HomeSceneService
from app.meetings.models import MeetingView
from app.meetings.service import MeetingService
from app.schemas.common import StrictModel
from app.tasks.brief import BriefView, DailyBriefService
from app.tasks.review import DailyReviewService, ReviewView
from app.workflows.models import WorkflowView
from app.workflows.service import WorkflowService

from .admin_config import AdminTokenGuard


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


def create_admin_butler_router(
    *,
    database: Database,
    workflows: WorkflowService,
    scenes: HomeSceneService,
    meetings: MeetingService,
    briefs: DailyBriefService,
    reviews: DailyReviewService,
    admin_token: str | None,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/butler",
        tags=["admin-butler"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    async def resolve_user(user_id: UUID | None) -> UUID:
        if user_id is not None:
            return user_id
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

    @router.get("/summary", response_model=AdminButlerSummary)
    async def summary(user_id: UUID | None = None) -> AdminButlerSummary:
        owner = await resolve_user(user_id)
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
        user_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[WorkflowView]:
        return await workflows.list_workflows(await resolve_user(user_id), limit=limit)

    @router.delete("/workflows/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_workflow(workflow_id: UUID, user_id: UUID | None = None) -> None:
        try:
            await workflows.delete_workflow(await resolve_user(user_id), workflow_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @router.get("/scenes", response_model=list[HomeSceneView])
    async def list_scenes(user_id: UUID | None = None) -> list[HomeSceneView]:
        return await scenes.list_scenes(await resolve_user(user_id))

    @router.post("/scenes/{scene_id}/enable", response_model=HomeSceneView)
    async def enable_scene(scene_id: UUID, user_id: UUID | None = None) -> HomeSceneView:
        try:
            return await scenes.set_enabled(await resolve_user(user_id), scene_id, enabled=True)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @router.post("/scenes/{scene_id}/disable", response_model=HomeSceneView)
    async def disable_scene(scene_id: UUID, user_id: UUID | None = None) -> HomeSceneView:
        try:
            return await scenes.set_enabled(await resolve_user(user_id), scene_id, enabled=False)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @router.delete("/scenes/{scene_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_scene(scene_id: UUID, user_id: UUID | None = None) -> None:
        try:
            await scenes.delete_scene(await resolve_user(user_id), scene_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @router.get("/meetings", response_model=list[MeetingView])
    async def list_meetings(
        user_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[MeetingView]:
        return await meetings.list(await resolve_user(user_id), limit=limit)

    @router.get("/briefs", response_model=list[BriefView])
    async def list_briefs(
        user_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=30)] = 14,
    ) -> list[BriefView]:
        return await briefs.recent_briefs(await resolve_user(user_id), limit=limit)

    @router.get("/reviews", response_model=list[ReviewView])
    async def list_reviews(
        user_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=30)] = 14,
    ) -> list[ReviewView]:
        return await reviews.recent_reviews(await resolve_user(user_id), limit=limit)

    return router
