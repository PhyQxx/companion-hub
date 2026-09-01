"""BRIEF-01 用户简报 API：查看最新/近期简报（含事实来源）与手动生成。"""

# 注意：不要加 `from __future__ import annotations`——FastAPI 0.141 的懒路由
# 在延迟求值注解时无法解析 `Depends(guard)`，会把 principal 降级成 query 参数。

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field

from app.auth import AuthService, ChatPrincipal
from app.schemas.common import StrictModel
from app.tasks.brief import BriefView, DailyBriefService

from .auth import ChatSessionGuard


class BriefListResponse(StrictModel):
    items: Annotated[list[BriefView], Field(max_length=31)]


def create_briefs_router(service: DailyBriefService, auth_service: AuthService) -> APIRouter:
    guard = ChatSessionGuard(auth_service)
    router = APIRouter(prefix="/api/v1/briefs", tags=["briefs"])

    @router.get("/latest", response_model=BriefView | None)
    async def latest_brief(
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> BriefView | None:
        return await service.latest_brief(principal.user_id)

    @router.get("", response_model=BriefListResponse)
    async def list_briefs(
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> BriefListResponse:
        return BriefListResponse(items=await service.recent_briefs(principal.user_id, limit=30))

    @router.post("/generate", response_model=BriefView)
    async def generate_brief(
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> BriefView:
        """手动生成当日简报（幂等，不投递）；用于预览事实与结论。"""
        try:
            return await service.build(principal.user_id)
        except Exception as error:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail="brief generation failed"
            ) from error

    return router
