from __future__ import annotations

import hmac
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import DatabaseConfigStore, DatabaseConfigVersion, HubConfig
from app.config.store import ConfigSnapshot, hash_config
from app.schemas.common import StrictModel

_BEARER = HTTPBearer(auto_error=False)
AdminCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)]


class AdminTokenGuard:
    def __init__(self, token: str | None) -> None:
        self._token = token

    async def __call__(self, credentials: AdminCredentials) -> None:
        if not self._token:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="admin API is disabled until ARIA_ADMIN_TOKEN is configured",
            )
        if credentials is None or not hmac.compare_digest(
            credentials.credentials, self._token
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid admin credential")


class ConfigVersionView(StrictModel):
    version: int
    status: str
    content_hash: str
    created_by: str
    created_at: datetime
    published_at: datetime | None
    rollback_from_version: int | None
    config: HubConfig | None = None


class CurrentConfigView(StrictModel):
    version: int
    content_hash: str
    published_at: datetime
    rollback_from_version: int | None
    config: HubConfig


class ValidationResult(StrictModel):
    valid: bool
    content_hash: str


def _version_view(
    version: DatabaseConfigVersion,
    *,
    include_config: bool = False,
) -> ConfigVersionView:
    return ConfigVersionView(
        version=version.version,
        status=version.status,
        content_hash=version.content_hash,
        created_by=version.created_by,
        created_at=version.created_at,
        published_at=version.published_at,
        rollback_from_version=version.rollback_from_version,
        config=version.config if include_config else None,
    )


def _current_view(snapshot: ConfigSnapshot) -> CurrentConfigView:
    return CurrentConfigView(
        version=snapshot.version,
        content_hash=snapshot.content_hash,
        published_at=snapshot.published_at,
        rollback_from_version=snapshot.rollback_from,
        config=snapshot.config,
    )


def create_admin_config_router(
    store: DatabaseConfigStore,
    *,
    admin_token: str | None,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/config",
        tags=["admin-config"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    @router.get("/current", response_model=CurrentConfigView)
    async def current() -> CurrentConfigView:
        return _current_view(store.current)

    @router.get("/versions", response_model=list[ConfigVersionView])
    async def versions() -> list[ConfigVersionView]:
        return [_version_view(version) for version in await store.list_versions()]

    @router.get("/versions/{version}", response_model=ConfigVersionView)
    async def version_detail(version: int) -> ConfigVersionView:
        try:
            result = await store.get_version(version)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        return _version_view(result, include_config=True)

    @router.post("/validate", response_model=ValidationResult)
    async def validate(config: HubConfig) -> ValidationResult:
        await store.validate(config)
        return ValidationResult(valid=True, content_hash=hash_config(config))

    @router.post(
        "/versions",
        response_model=ConfigVersionView,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_draft(config: HubConfig) -> ConfigVersionView:
        result = await store.create_draft(config, actor="admin")
        return _version_view(result, include_config=True)

    @router.post("/versions/{version}/publish", response_model=CurrentConfigView)
    async def publish(version: int) -> CurrentConfigView:
        try:
            snapshot = await store.publish(version, actor="admin")
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        return _current_view(snapshot)

    @router.post("/versions/{version}/rollback", response_model=CurrentConfigView)
    async def rollback(version: int) -> CurrentConfigView:
        try:
            snapshot = await store.rollback(version, actor="admin")
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        return _current_view(snapshot)

    return router
