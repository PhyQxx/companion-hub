from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import Field

from app.avatar import (
    AvatarAssetImporter,
    AvatarBindingView,
    AvatarImportResult,
    AvatarInstanceView,
    AvatarPackView,
    AvatarStore,
)
from app.schemas.common import StrictModel

from .admin_config import AdminTokenGuard


class PackItem(StrictModel):
    id: str
    name: str
    archetype: str
    engine: str
    manifest: dict[str, Any]
    content_hash: str
    license: dict[str, Any]
    built_in: bool
    installed_at: datetime


class InstanceItem(StrictModel):
    id: str
    owner: str
    name: str
    pack_id: str
    customization: dict[str, Any]
    voice_profile_id: str | None
    theme_id: str | None
    version: int
    status: str
    created_at: datetime


class BindingItem(StrictModel):
    persona_id: int
    avatar: InstanceItem
    is_default: bool
    created_at: datetime


class CreateInstanceRequest(StrictModel):
    pack_id: str
    name: str = Field(min_length=1, max_length=160)
    customization: dict[str, Any] = Field(default_factory=dict)
    voice_profile_id: str | None = None
    theme_id: str | None = None


class UpdateInstanceRequest(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    customization: dict[str, Any] | None = None
    voice_profile_id: str | None = None
    theme_id: str | None = None
    status: Literal["active", "preview", "archived"] | None = None


class BindRequest(StrictModel):
    persona_id: int
    is_default: bool = False


class ImportResultItem(StrictModel):
    pack: PackItem
    instance: InstanceItem


def _pack_item(view: AvatarPackView) -> PackItem:
    return PackItem(
        id=view.id,
        name=view.name,
        archetype=view.archetype,
        engine=view.engine,
        manifest=view.manifest,
        content_hash=view.content_hash,
        license=view.license,
        built_in=view.built_in,
        installed_at=view.installed_at,
    )


def _binding_item(view: AvatarBindingView) -> BindingItem:
    return BindingItem(
        persona_id=view.persona_id,
        avatar=_instance_item(view.avatar),
        is_default=view.is_default,
        created_at=view.created_at,
    )


def _instance_item(view: AvatarInstanceView) -> InstanceItem:
    return InstanceItem(
        id=str(view.id),
        owner=view.owner,
        name=view.name,
        pack_id=view.pack_id,
        customization=view.customization,
        voice_profile_id=view.voice_profile_id,
        theme_id=str(view.theme_id) if view.theme_id is not None else None,
        version=view.version,
        status=view.status,
        created_at=view.created_at,
    )


def _import_result_item(result: AvatarImportResult) -> ImportResultItem:
    return ImportResultItem(pack=_pack_item(result.pack), instance=_instance_item(result.instance))


def create_admin_avatar_router(
    avatar_store: AvatarStore,
    *,
    admin_token: str | None,
    asset_importer: AvatarAssetImporter | None = None,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/avatars",
        tags=["admin-avatars"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    @router.get("/packs", response_model=list[PackItem])
    async def list_packs() -> list[PackItem]:
        packs = await avatar_store.list_packs()
        return [_pack_item(p) for p in packs]

    @router.post(
        "/imports/static",
        response_model=ImportResultItem,
        status_code=status.HTTP_201_CREATED,
    )
    async def import_static_avatar(
        name: Annotated[str, Form(min_length=1, max_length=160)],
        rights_confirmed: Annotated[bool, Form()],
        file: Annotated[UploadFile, File()],
    ) -> ImportResultItem:
        if asset_importer is None:
            raise HTTPException(status_code=503, detail="avatar asset import is not configured")
        data = await file.read(10 * 1024 * 1024 + 1)
        try:
            return _import_result_item(
                await asset_importer.import_static(
                    data,
                    name=name,
                    rights_confirmed=rights_confirmed,
                )
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.post(
        "/imports/live2d",
        response_model=ImportResultItem,
        status_code=status.HTTP_201_CREATED,
    )
    async def import_live2d_avatar(
        name: Annotated[str, Form(min_length=1, max_length=160)],
        rights_confirmed: Annotated[bool, Form()],
        file: Annotated[UploadFile, File()],
    ) -> ImportResultItem:
        if asset_importer is None:
            raise HTTPException(status_code=503, detail="avatar asset import is not configured")
        data = await file.read(50 * 1024 * 1024 + 1)
        try:
            return _import_result_item(
                await asset_importer.import_live2d(
                    data,
                    name=name,
                    rights_confirmed=rights_confirmed,
                )
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/instances", response_model=list[InstanceItem])
    async def list_instances(
        status: Annotated[str | None, Query()] = None,
    ) -> list[InstanceItem]:
        instances = await avatar_store.list_instances(status=status)
        return [_instance_item(i) for i in instances]

    @router.post("/instances", response_model=InstanceItem, status_code=status.HTTP_201_CREATED)
    async def create_instance(body: CreateInstanceRequest) -> InstanceItem:
        try:
            instance = await avatar_store.create_instance(
                pack_id=body.pack_id,
                name=body.name,
                customization=body.customization,
                voice_profile_id=body.voice_profile_id,
                theme_id=UUID(body.theme_id) if body.theme_id is not None else None,
            )
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        return _instance_item(instance)

    @router.get("/instances/{instance_id}", response_model=InstanceItem)
    async def get_instance(instance_id: UUID) -> InstanceItem:
        instance = await avatar_store.get_instance(instance_id)
        if instance is None:
            raise HTTPException(status_code=404, detail="instance not found")
        return _instance_item(instance)

    @router.patch("/instances/{instance_id}", response_model=InstanceItem)
    async def update_instance(
        instance_id: UUID,
        body: UpdateInstanceRequest,
    ) -> InstanceItem:
        try:
            updated = await avatar_store.update_instance(
                instance_id,
                name=body.name,
                customization=body.customization,
                voice_profile_id=body.voice_profile_id,
                theme_id=UUID(body.theme_id) if body.theme_id is not None else None,
                status=body.status,
                update_voice_profile="voice_profile_id" in body.model_fields_set,
                update_theme="theme_id" in body.model_fields_set,
            )
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        if updated is None:
            raise HTTPException(status_code=404, detail="instance not found")
        return _instance_item(updated)

    @router.delete("/instances/{instance_id}")
    async def delete_instance(instance_id: UUID) -> dict[str, bool]:
        try:
            ok = await avatar_store.delete_instance(instance_id)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        if not ok:
            raise HTTPException(status_code=404, detail="instance not found")
        return {"deleted": True}

    @router.post("/instances/{instance_id}/bind")
    async def bind_instance(
        instance_id: UUID,
        body: BindRequest,
    ) -> dict[str, bool]:
        try:
            ok = await avatar_store.bind_to_persona(
                body.persona_id, instance_id, is_default=body.is_default
            )
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return {"bound": ok}

    @router.delete("/instances/{instance_id}/bindings/{persona_id}")
    async def unbind_instance(instance_id: UUID, persona_id: int) -> dict[str, bool]:
        ok = await avatar_store.unbind_from_persona(persona_id, instance_id)
        if not ok:
            raise HTTPException(status_code=404, detail="avatar binding not found")
        return {"unbound": True}

    @router.get("/bindings", response_model=list[BindingItem])
    async def list_bindings(
        persona_id: Annotated[int | None, Query()] = None,
    ) -> list[BindingItem]:
        bindings = await avatar_store.list_bindings(persona_id=persona_id)
        return [_binding_item(binding) for binding in bindings]

    @router.get("/persona/{persona_id}/default", response_model=InstanceItem | None)
    async def get_default_for_persona(persona_id: int) -> InstanceItem | None:
        instance = await avatar_store.get_default_for_persona(persona_id)
        return _instance_item(instance) if instance is not None else None

    return router
