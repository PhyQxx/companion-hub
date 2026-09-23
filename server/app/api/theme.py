from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException

from app.appearance import ThemePreferenceView, ThemeStore, ThemeView
from app.auth import AuthService, ChatPrincipal
from app.schemas.common import StrictModel

from .admin_config import AdminTokenGuard
from .auth import ChatSessionGuard


class ThemeItem(StrictModel):
    id: str
    key: str
    name: str
    mode: str
    schema_version: int
    version: int
    definition: dict[str, Any]
    content_hash: str
    built_in: bool
    updated_at: datetime


class ThemeSchedule(StrictModel):
    """定时主题切换边界：light_time 起用明亮、dark_time 起用深色（HH:MM）。"""

    light_time: str
    dark_time: str


class ThemePreferenceItem(StrictModel):
    owner: str
    selection: Literal["pure-light", "midnight-violet", "system", "scheduled"]
    theme: ThemeItem
    appearance_mode: str
    updated_at: datetime | None
    schedule: ThemeSchedule | None = None


class UpdateThemePreferenceRequest(StrictModel):
    selection: Literal["pure-light", "midnight-violet", "system", "scheduled"]
    light_time: str = "07:00"
    dark_time: str = "19:00"


def create_admin_theme_router(store: ThemeStore, *, admin_token: str | None) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/ui",
        tags=["admin-ui"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    @router.get("/themes", response_model=list[ThemeItem])
    async def list_themes() -> list[ThemeItem]:
        return [_theme_item(item) for item in await store.list_themes()]

    @router.get("/preferences", response_model=ThemePreferenceItem)
    async def get_preference() -> ThemePreferenceItem:
        return _preference_item(await store.get_preference())

    @router.put("/preferences", response_model=ThemePreferenceItem)
    async def update_preference(body: UpdateThemePreferenceRequest) -> ThemePreferenceItem:
        try:
            return _preference_item(
                await store.set_preference(
                    body.selection,
                    light_time=body.light_time,
                    dark_time=body.dark_time,
                )
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    return router


def create_theme_router(store: ThemeStore, auth_service: AuthService) -> APIRouter:
    guard = ChatSessionGuard(auth_service)
    guard_dependency = Depends(guard)
    router = APIRouter(prefix="/api/v1/ui", tags=["ui"])

    @router.get("/themes", response_model=list[ThemeItem])
    async def list_themes(
        _principal: ChatPrincipal = guard_dependency,
    ) -> list[ThemeItem]:
        return [_theme_item(item) for item in await store.list_themes()]

    @router.get("/preferences", response_model=ThemePreferenceItem)
    async def get_preference(
        _principal: ChatPrincipal = guard_dependency,
    ) -> ThemePreferenceItem:
        return _preference_item(await store.get_preference())

    @router.put("/preferences", response_model=ThemePreferenceItem)
    async def update_preference(
        body: UpdateThemePreferenceRequest,
        _principal: ChatPrincipal = guard_dependency,
    ) -> ThemePreferenceItem:
        try:
            return _preference_item(
                await store.set_preference(
                    body.selection,
                    light_time=body.light_time,
                    dark_time=body.dark_time,
                )
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    return router


def _theme_item(view: ThemeView) -> ThemeItem:
    return ThemeItem(
        id=str(view.id),
        key=view.key,
        name=view.name,
        mode=view.mode,
        schema_version=view.schema_version,
        version=view.version,
        definition=view.definition,
        content_hash=view.content_hash,
        built_in=view.built_in,
        updated_at=view.updated_at,
    )


def _preference_item(view: ThemePreferenceView) -> ThemePreferenceItem:
    return ThemePreferenceItem(
        owner=view.owner,
        selection=view.selection,
        theme=_theme_item(view.theme),
        appearance_mode=view.appearance_mode,
        updated_at=view.updated_at,
        schedule=ThemeSchedule(light_time=view.schedule[0], dark_time=view.schedule[1])
        if view.schedule
        else None,
    )
