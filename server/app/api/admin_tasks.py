"""TASK-01 Admin 任务可视化：跨用户分页列表、状态计数与运维取消。

只读为主；取消用于处理卡在 firing/active 的任务（如投递通道长期不可用），
不提供代替用户完成/稍后语义的入口。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.schemas.common import StrictModel
from app.tasks import TaskKind, TaskStatus, TaskStore, TaskView

from .admin_config import AdminTokenGuard


class AdminTaskListResponse(StrictModel):
    items: list[TaskView]
    total: int
    limit: int
    offset: int
    counts: dict[str, int]


def create_admin_tasks_router(store: TaskStore, *, admin_token: str | None) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/tasks",
        tags=["admin-tasks"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    @router.get("", response_model=AdminTaskListResponse)
    async def list_tasks(
        user_id: UUID | None = None,
        status_filter: Annotated[str | None, Query(alias="status")] = None,
        kind: str | None = None,
        source: Annotated[str | None, Query(max_length=16)] = None,
        query: Annotated[str, Query(max_length=120)] = "",
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> AdminTaskListResponse:
        try:
            task_status = TaskStatus(status_filter) if status_filter else None
            task_kind = TaskKind(kind) if kind else None
        except ValueError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"invalid filter: {error}"
            ) from error
        items = await store.admin_list_tasks(
            user_id=user_id,
            status=task_status,
            kind=task_kind,
            source=source,
            query=query.strip() or None,
            limit=limit,
            offset=offset,
        )
        total = await store.admin_count_tasks(
            user_id=user_id,
            status=task_status,
            kind=task_kind,
            source=source,
            query=query.strip() or None,
        )
        counts = await store.admin_status_counts(user_id=user_id)
        return AdminTaskListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            counts=counts,
        )

    @router.post("/{task_id}/cancel", response_model=TaskView)
    async def cancel_task(task_id: UUID, user_id: UUID) -> TaskView:
        try:
            return await store.cancel_task(user_id, task_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error

    return router
