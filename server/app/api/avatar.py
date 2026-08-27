from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from app.auth import AuthService, ChatPrincipal
from app.avatar import AvatarInstanceView, AvatarPackView, AvatarStore
from app.persona import PersonaStore
from app.schemas.common import StrictModel

from .auth import ChatSessionGuard


class AvatarChoiceItem(StrictModel):
    instance_id: str
    pack_id: str
    name: str
    engine: str
    status: str
    is_current: bool
    customization: dict[str, Any]
    manifest: dict[str, Any]


class SwitchAvatarRequest(StrictModel):
    instance_id: UUID


def _choice(
    instance: AvatarInstanceView,
    pack: AvatarPackView,
    *,
    current_id: UUID | None,
) -> AvatarChoiceItem:
    return AvatarChoiceItem(
        instance_id=str(instance.id),
        pack_id=instance.pack_id,
        name=instance.name,
        engine=pack.engine,
        status=instance.status,
        is_current=instance.id == current_id,
        customization=instance.customization,
        manifest=pack.manifest,
    )


def create_avatar_router(
    avatar_store: AvatarStore,
    persona_store: PersonaStore,
    auth_service: AuthService,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/avatars", tags=["avatars"])
    guard = ChatSessionGuard(auth_service)
    guard_dependency = Depends(guard)

    @router.get("", response_model=list[AvatarChoiceItem])
    async def list_avatar_choices(
        _principal: ChatPrincipal = guard_dependency,
    ) -> list[AvatarChoiceItem]:
        persona = await persona_store.refresh()
        current = await avatar_store.get_default_for_persona(persona.version)
        instances = await avatar_store.list_instances(owner="local-user", status="active")
        result: list[AvatarChoiceItem] = []
        for instance in instances:
            pack = await avatar_store.get_pack(instance.pack_id)
            if pack is not None:
                result.append(
                    _choice(
                        instance,
                        pack,
                        current_id=current.id if current is not None else None,
                    )
                )
        return result

    @router.put("/current", response_model=AvatarChoiceItem)
    async def switch_current_avatar(
        body: SwitchAvatarRequest,
        _principal: ChatPrincipal = guard_dependency,
    ) -> AvatarChoiceItem:
        instance = await avatar_store.get_instance(body.instance_id)
        if instance is None:
            raise HTTPException(status_code=404, detail="avatar instance not found")
        if instance.status != "active":
            raise HTTPException(status_code=409, detail="only an active avatar can be selected")
        pack = await avatar_store.get_pack(instance.pack_id)
        if pack is None:
            raise HTTPException(status_code=409, detail="avatar pack is unavailable")
        persona = await persona_store.refresh()
        await avatar_store.bind_to_persona(persona.version, instance.id, is_default=True)
        return _choice(instance, pack, current_id=instance.id)

    return router
