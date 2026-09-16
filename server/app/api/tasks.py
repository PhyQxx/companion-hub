"""TASK-01 用户任务 API：创建/查询/完成/取消/稍后提醒。

注意：不要加 `from __future__ import annotations`——FastAPI 0.141 的懒路由
在延迟求值注解时无法解析 `Depends(guard)`，会把 principal 降级成 query 参数。
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field

from app.auth import AuthService, ChatPrincipal
from app.schemas.common import PrivacyLevel, StrictModel
from app.tasks import TaskKind, TaskStatus, TaskStore, TaskTrigger, TaskView

from .auth import ChatSessionGuard


class CreateTaskRequest(StrictModel):
    kind: Literal["reminder", "task"] = "reminder"
    title: Annotated[str, Field(min_length=1, max_length=320)]
    notes: Annotated[str, Field(max_length=2000)] | None = None
    trigger: TaskTrigger
    privacy_level: Literal["L0", "L1"] = "L1"


class SnoozeTaskRequest(StrictModel):
    minutes: Annotated[int, Field(ge=1, le=10_080)] | None = None
    until: datetime | None = None


class UpdateTaskRequest(StrictModel):
    """TODO-01 反向推送入口：优先级（任意任务）与延期（仅 pnkx 镜像）。"""

    priority: Annotated[int, Field(ge=0, le=3)] | None = None
    clear_priority: bool = False
    defer_until: datetime | None = None


def create_tasks_router(store: TaskStore, auth_service: AuthService) -> APIRouter:
    guard = ChatSessionGuard(auth_service)
    router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

    @router.post("", response_model=TaskView, status_code=status.HTTP_201_CREATED)
    async def create_task(
        body: CreateTaskRequest,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> TaskView:
        try:
            return await store.create(
                user_id=principal.user_id,
                kind=TaskKind(body.kind),
                title=body.title,
                notes=body.notes,
                trigger=body.trigger,
                privacy_level=PrivacyLevel(body.privacy_level),
            )
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    @router.get("", response_model=list[TaskView])
    async def list_tasks(
        principal: Annotated[ChatPrincipal, Depends(guard)],
        status_filter: Annotated[str | None, Field(alias="status")] = None,
    ) -> list[TaskView]:
        task_status = TaskStatus(status_filter) if status_filter else None
        return await store.list_tasks(principal.user_id, status=task_status)

    @router.get("/{task_id}", response_model=TaskView)
    async def get_task(
        task_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> TaskView:
        try:
            return await store.get_task(principal.user_id, task_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @router.patch("/{task_id}", response_model=TaskView)
    async def update_task(
        task_id: UUID,
        body: UpdateTaskRequest,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> TaskView:
        if body.priority is None and not body.clear_priority and body.defer_until is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="必须提供 priority、clear_priority 或 defer_until",
            )
        try:
            return await store.update_fields(
                principal.user_id,
                task_id,
                priority=body.priority,
                clear_priority=body.clear_priority,
                defer_until=body.defer_until,
            )
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    @router.post("/{task_id}/complete", response_model=TaskView)
    async def complete_task(
        task_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> TaskView:
        try:
            return await store.complete_task(principal.user_id, task_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error

    @router.post("/{task_id}/cancel", response_model=TaskView)
    async def cancel_task(
        task_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> TaskView:
        try:
            return await store.cancel_task(principal.user_id, task_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error

    @router.post("/{task_id}/snooze", response_model=TaskView)
    async def snooze_task(
        task_id: UUID,
        body: SnoozeTaskRequest,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> TaskView:
        if body.minutes is None and body.until is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="必须提供 minutes 或 until",
            )
        until = body.until or datetime.now(UTC) + timedelta(minutes=body.minutes or 0)
        try:
            return await store.snooze_task(principal.user_id, task_id, until=until)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error

    return router
