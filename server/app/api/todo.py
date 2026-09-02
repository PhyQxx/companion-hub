"""TODO-01 用户任务同步 API：手动触发一次同步并返回统计。"""

# 注意：不要加 `from __future__ import annotations`——FastAPI 0.141 的懒路由
# 在延迟求值注解时无法解析 `Depends(guard)`，会把 principal 降级成 query 参数。

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import AuthService, ChatPrincipal
from app.schemas.common import StrictModel
from app.todo import TodoSyncService

from .auth import ChatSessionGuard


class TodoSyncResponse(StrictModel):
    pulled: int
    mirrors_created: int
    mirrors_updated: int
    mirrors_cancelled: int
    completions_pushed: int
    new_pushed: int
    adopted_after_crash: int
    errors: list[str]


def create_todo_router(service: TodoSyncService, auth_service: AuthService) -> APIRouter:
    guard = ChatSessionGuard(auth_service)
    router = APIRouter(prefix="/api/v1/todo", tags=["todo"])

    @router.post("/sync", response_model=TodoSyncResponse)
    async def trigger_sync(
        _: Annotated[ChatPrincipal, Depends(guard)],
    ) -> TodoSyncResponse:
        try:
            stats = await service.sync_once()
        except Exception as error:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail="todo sync failed"
            ) from error
        return TodoSyncResponse(**asdict(stats))

    return router
