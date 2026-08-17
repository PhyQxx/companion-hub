from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status

from app.persona import (
    PersonaConfig,
    PersonaSnapshot,
    PersonaStore,
    PersonaVersion,
    hash_persona,
)
from app.schemas.common import StrictModel

from .admin_config import AdminTokenGuard


class PersonaVersionView(StrictModel):
    version: int
    status: str
    content_hash: str
    created_by: str
    created_at: datetime
    published_at: datetime | None
    rollback_from_version: int | None
    persona: PersonaConfig | None = None


class CurrentPersonaView(StrictModel):
    version: int
    content_hash: str
    published_at: datetime
    rollback_from_version: int | None
    persona: PersonaConfig


def _version_view(value: PersonaVersion, *, include_persona: bool = False) -> PersonaVersionView:
    return PersonaVersionView(
        version=value.version,
        status=value.status,
        content_hash=value.content_hash,
        created_by=value.created_by,
        created_at=value.created_at,
        published_at=value.published_at,
        rollback_from_version=value.rollback_from_version,
        persona=value.persona if include_persona else None,
    )


def _current_view(value: PersonaSnapshot) -> CurrentPersonaView:
    return CurrentPersonaView(
        version=value.version,
        content_hash=value.content_hash,
        published_at=value.published_at,
        rollback_from_version=value.rollback_from,
        persona=value.persona,
    )


def create_admin_persona_router(store: PersonaStore, *, admin_token: str | None) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/personas",
        tags=["admin-personas"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    @router.get("/current", response_model=CurrentPersonaView)
    async def current() -> CurrentPersonaView:
        return _current_view(store.current)

    @router.get("/versions", response_model=list[PersonaVersionView])
    async def versions() -> list[PersonaVersionView]:
        return [_version_view(value) for value in await store.list_versions()]

    @router.get("/versions/{version}", response_model=PersonaVersionView)
    async def version_detail(version: int) -> PersonaVersionView:
        try:
            return _version_view(await store.get_version(version), include_persona=True)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @router.post("/validate")
    async def validate(persona: PersonaConfig) -> dict[str, object]:
        return {"valid": True, "content_hash": hash_persona(persona)}

    @router.post(
        "/versions", response_model=PersonaVersionView, status_code=status.HTTP_201_CREATED
    )
    async def create_draft(persona: PersonaConfig) -> PersonaVersionView:
        return _version_view(
            await store.create_draft(persona, actor="admin"), include_persona=True
        )

    @router.post("/versions/{version}/publish", response_model=CurrentPersonaView)
    async def publish(version: int) -> CurrentPersonaView:
        try:
            return _current_view(await store.publish(version))
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error

    @router.post("/versions/{version}/rollback", response_model=CurrentPersonaView)
    async def rollback(version: int) -> CurrentPersonaView:
        try:
            return _current_view(await store.rollback(version, actor="admin"))
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error

    return router
