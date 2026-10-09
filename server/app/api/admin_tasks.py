"""TASK-01 Admin 任务可视化：任务分页列表、状态计数与运维取消。

只读为主；取消用于处理卡在 firing/active 的任务（如投递通道长期不可用），
不提供代替用户完成/稍后语义的入口。

多用户：个人数据路由；会话访问 scope 到本人（业主可显式指定 user_id），
admin token 机器访问 user_id 缺省为跨用户全量（运维视角）。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.schemas.common import StrictModel
from app.tasks import TaskKind, TaskStatus, TaskStore, TaskView

from .admin_config import AdminTokenGuard, admin_access


class AdminTaskListResponse(StrictModel):
    items: list[TaskView]
    total: int
    limit: int
    offset: int
    counts: dict[str, int]


def create_admin_tasks_router(store: TaskStore, *, admin_token: str | None) -> APIRouter:
    guard = AdminTokenGuard(admin_token, min_role="member")
    router = APIRouter(
        prefix="/api/v1/admin/tasks",
        tags=["admin-tasks"],
        dependencies=[Depends(guard)],
    )

    def scoped_user(request: Request, user_id: UUID | None = None) -> UUID | None:
        return admin_access(request).scoped_user_id(user_id)

    def required_user(request: Request, user_id: UUID | None = None) -> UUID:
        scoped = admin_access(request).scoped_user_id(user_id)
        if scoped is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="user_id required")
        return scoped

    @router.get("", response_model=AdminTaskListResponse)
    async def list_tasks(
        request: Request,
        user_id: UUID | None = None,
        status_filter: Annotated[str | None, Query(alias="status")] = None,
        kind: str | None = None,
        source: Annotated[str | None, Query(max_length=16)] = None,
        query: Annotated[str, Query(max_length=120)] = "",
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> AdminTaskListResponse:
        user_id = scoped_user(request, user_id)
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
    async def cancel_task(
        task_id: UUID, request: Request, user_id: UUID | None = None
    ) -> TaskView:
        try:
            return await store.cancel_task(required_user(request, user_id), task_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error

    return router
