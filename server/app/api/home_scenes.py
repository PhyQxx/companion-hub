"""HOME-01 用户场景 API：列表/创建/详情/启停/删除/手动运行。

注意：不要加 `from __future__ import annotations`——FastAPI 0.141 的懒路由
在延迟求值注解时无法解析 `Depends(guard)`，会把 principal 降级成 query 参数。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field

from app.auth import AuthService, ChatPrincipal
from app.home_scene import (
    MAX_SCENE_STEPS,
    HomeSceneStep,
    HomeSceneTriggered,
    HomeSceneView,
)
from app.home_scene.service import HomeSceneService
from app.schemas.common import StrictModel

from .auth import ChatSessionGuard


class HomeScenePayload(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    trigger: Annotated[str, Field(min_length=1, max_length=120)]
    window_start: Annotated[str, Field(pattern=r"^\d{1,2}:\d{2}$")] | None = None
    window_end: Annotated[str, Field(pattern=r"^\d{1,2}:\d{2}$")] | None = None
    steps: Annotated[list[HomeSceneStep], Field(min_length=1, max_length=MAX_SCENE_STEPS)]


def create_home_scenes_router(service: HomeSceneService, auth_service: AuthService) -> APIRouter:
    guard = ChatSessionGuard(auth_service)
    router = APIRouter(prefix="/api/v1/home/scenes", tags=["home-scenes"])

    @router.get("", response_model=list[HomeSceneView])
    async def list_scenes(
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> list[HomeSceneView]:
        return await service.list_scenes(principal.user_id)

    @router.post("", response_model=HomeSceneView, status_code=status.HTTP_201_CREATED)
    async def create_scene(
        body: HomeScenePayload,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> HomeSceneView:
        try:
            return await service.create_scene(
                user_id=principal.user_id,
                name=body.name,
                trigger=body.trigger,
                steps=body.steps,
                window_start=body.window_start,
                window_end=body.window_end,
            )
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    @router.get("/{scene_id}", response_model=HomeSceneView)
    async def get_scene(
        scene_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> HomeSceneView:
        try:
            return await service.get_scene(principal.user_id, scene_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @router.post("/{scene_id}/enable", response_model=HomeSceneView)
    async def enable_scene(
        scene_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> HomeSceneView:
        try:
            return await service.set_enabled(principal.user_id, scene_id, enabled=True)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @router.post("/{scene_id}/disable", response_model=HomeSceneView)
    async def disable_scene(
        scene_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> HomeSceneView:
        try:
            return await service.set_enabled(principal.user_id, scene_id, enabled=False)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @router.post("/{scene_id}/run", response_model=HomeSceneTriggered)
    async def run_scene(
        scene_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> HomeSceneTriggered:
        try:
            triggered = await service.run_manual(principal.user_id, scene_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
        if triggered is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="home scene not found")
        return triggered

    @router.delete("/{scene_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_scene(
        scene_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> None:
        try:
            await service.delete_scene(principal.user_id, scene_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    return router
