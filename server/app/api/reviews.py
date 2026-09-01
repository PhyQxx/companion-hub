"""REVIEW-01 用户回顾 API：查看/生成晚间回顾与逐项修正。"""

# 注意：不要加 `from __future__ import annotations`——FastAPI 0.141 的懒路由
# 在延迟求值注解时无法解析 `Depends(guard)`，会把 principal 降级成 query 参数。

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field

from app.auth import AuthService, ChatPrincipal
from app.schemas.common import StrictModel
from app.tasks.review import DailyReviewService, ReviewView

from .auth import ChatSessionGuard


class CorrectReviewItemRequest(StrictModel):
    action: Literal["confirmed", "removed"]
    note: Annotated[str, Field(min_length=1, max_length=200)] | None = None


def create_reviews_router(service: DailyReviewService, auth_service: AuthService) -> APIRouter:
    guard = ChatSessionGuard(auth_service)
    router = APIRouter(prefix="/api/v1/reviews", tags=["reviews"])

    @router.get("/latest", response_model=ReviewView | None)
    async def latest_review(
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> ReviewView | None:
        return await service.latest_review(principal.user_id)

    @router.post("/generate", response_model=ReviewView)
    async def generate_review(
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> ReviewView:
        """手动生成当晚回顾（幂等，不覆盖已有修正，不投递）。"""
        try:
            return await service.build(principal.user_id)
        except Exception as error:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail="review generation failed"
            ) from error

    @router.patch("/{review_id}/items/{index}", response_model=ReviewView)
    async def correct_review_item(
        review_id: UUID,
        index: int,
        body: CorrectReviewItemRequest,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> ReviewView:
        """逐项修正：确认/移除/附注只作用于回顾自身，不改写任何真源数据。"""
        try:
            return await service.correct_item(
                principal.user_id,
                review_id,
                index,
                action=body.action,
                note=body.note,
            )
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    return router
