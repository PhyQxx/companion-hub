"""FLOW-01 用户流程 API：列表/创建/详情/删除/运行。

运行 = 展开为待确认 ActionPlan（沿用 /api/v1/cognition/action-plans 的
确认/取消/执行流）；本路由不直接执行计划。创建接口在保存前已由服务层
完成注册表编译校验，未知动作或参数漂移返回 422。

注意：不要加 `from __future__ import annotations`——FastAPI 0.141 的懒路由
在延迟求值注解时无法解析 `Depends(guard)`，会把 principal 降级成 query 参数。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field

from app.auth import AuthService, ChatPrincipal
from app.schemas.common import StrictModel
from app.workflows import (
    WorkflowPreview,
    WorkflowRunView,
    WorkflowSaveTool,
    WorkflowService,
    WorkflowStep,
    WorkflowView,
)

from .auth import ChatSessionGuard


class WorkflowPayload(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    description: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    steps: Annotated[list[WorkflowStep], Field(min_length=1, max_length=10)]


class WorkflowDraftConfirmation(StrictModel):
    digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def create_workflows_router(
    service: WorkflowService,
    auth_service: AuthService,
    save_tool: WorkflowSaveTool | None = None,
) -> APIRouter:
    guard = ChatSessionGuard(auth_service)
    router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])

    if save_tool is not None:

        @router.get("/drafts")
        async def list_drafts(
            principal: Annotated[ChatPrincipal, Depends(guard)],
        ) -> list[dict[str, object]]:
            return save_tool.list_drafts(principal.user_id)

        @router.post("/drafts/{draft_id}/confirm")
        async def confirm_draft(
            draft_id: UUID,
            body: WorkflowDraftConfirmation,
            principal: Annotated[ChatPrincipal, Depends(guard)],
        ) -> dict[str, object]:
            try:
                return await save_tool.confirm(principal.user_id, draft_id, body.digest)
            except LookupError as error:
                raise HTTPException(404, str(error)) from error
            except ValueError as error:
                raise HTTPException(409, str(error)) from error

        @router.post("/drafts/{draft_id}/cancel")
        async def cancel_draft(
            draft_id: UUID,
            principal: Annotated[ChatPrincipal, Depends(guard)],
        ) -> dict[str, object]:
            try:
                return save_tool.cancel(principal.user_id, draft_id)
            except LookupError as error:
                raise HTTPException(404, str(error)) from error
            except ValueError as error:
                raise HTTPException(409, str(error)) from error

    @router.get("", response_model=list[WorkflowView])
    async def list_workflows(
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> list[WorkflowView]:
        return await service.list_workflows(principal.user_id)

    @router.post("/preview", response_model=WorkflowPreview)
    async def preview_workflow(
        body: WorkflowPayload,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> WorkflowPreview:
        """保存前展示：每步的动作标签、风险等级、确认策略与参数，不落库。"""
        try:
            return await service.preview(
                name=body.name,
                description=body.description,
                steps=body.steps,
            )
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    @router.post("", response_model=WorkflowView, status_code=status.HTTP_201_CREATED)
    async def create_workflow(
        body: WorkflowPayload,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> WorkflowView:
        try:
            return await service.save_workflow(
                user_id=principal.user_id,
                name=body.name,
                description=body.description,
                steps=body.steps,
            )
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    @router.get("/{workflow_id}", response_model=WorkflowView)
    async def get_workflow(
        workflow_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> WorkflowView:
        try:
            return await service.get_workflow(principal.user_id, workflow_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_workflow(
        workflow_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> None:
        try:
            await service.delete_workflow(principal.user_id, workflow_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @router.post("/{workflow_id}/run", response_model=WorkflowRunView)
    async def run_workflow(
        workflow_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> WorkflowRunView:
        try:
            return await service.run_workflow(principal.user_id, workflow_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    return router
