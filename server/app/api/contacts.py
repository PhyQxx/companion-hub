"""CONTACT-01 用户联系人 API：列表/搜索/创建/更新/删除。

注意：不要加 `from __future__ import annotations`——FastAPI 0.141 的懒路由
在延迟求值注解时无法解析 `Depends(guard)`，会把 principal 降级成 query 参数。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field

from app.auth import AuthService, ChatPrincipal
from app.contacts import (
    ContactImportantDate,
    ContactPreference,
    ContactStore,
    ContactView,
)
from app.schemas.common import StrictModel

from .auth import ChatSessionGuard


class ContactPayload(StrictModel):
    display_name: Annotated[str, Field(min_length=1, max_length=120)]
    aliases: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=12
    )
    relationship: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    timezone: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    important_dates: list[ContactImportantDate] = Field(default_factory=list, max_length=12)
    preferences: list[ContactPreference] = Field(default_factory=list, max_length=24)
    notes: Annotated[str, Field(max_length=2000)] | None = None


class ContactPatchPayload(StrictModel):
    display_name: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    aliases: list[Annotated[str, Field(min_length=1, max_length=80)]] | None = Field(
        default=None, max_length=12
    )
    relationship: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    # 传空字符串清除时区；省略表示不变
    timezone: Annotated[str, Field(min_length=0, max_length=64)] | None = None
    important_dates: list[ContactImportantDate] | None = Field(default=None, max_length=12)
    preferences: list[ContactPreference] | None = Field(default=None, max_length=24)
    notes: Annotated[str, Field(max_length=2000)] | None = None


def create_contacts_router(store: ContactStore, auth_service: AuthService) -> APIRouter:
    guard = ChatSessionGuard(auth_service)
    router = APIRouter(prefix="/api/v1/contacts", tags=["contacts"])

    @router.get("", response_model=list[ContactView])
    async def list_contacts(
        principal: Annotated[ChatPrincipal, Depends(guard)],
        q: Annotated[str | None, Query(max_length=120)] = None,
    ) -> list[ContactView]:
        return await store.list_contacts(principal.user_id, query=q)

    @router.post("", response_model=ContactView, status_code=status.HTTP_201_CREATED)
    async def create_contact(
        body: ContactPayload,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> ContactView:
        try:
            return await store.create_contact(
                user_id=principal.user_id,
                display_name=body.display_name,
                aliases=body.aliases,
                relationship=body.relationship,
                timezone=body.timezone,
                important_dates=body.important_dates,
                preferences=body.preferences,
                notes=body.notes,
            )
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    @router.get("/{contact_id}", response_model=ContactView)
    async def get_contact(
        contact_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> ContactView:
        try:
            return await store.get_contact_view(principal.user_id, contact_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @router.patch("/{contact_id}", response_model=ContactView)
    async def patch_contact(
        contact_id: UUID,
        body: ContactPatchPayload,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> ContactView:
        try:
            return await store.update_contact(
                principal.user_id,
                contact_id,
                display_name=body.display_name,
                aliases=body.aliases,
                relationship=body.relationship,
                timezone=body.timezone,
                important_dates=body.important_dates,
                preferences=body.preferences,
                notes=body.notes,
            )
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    @router.delete("/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_contact(
        contact_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> None:
        try:
            await store.delete_contact(principal.user_id, contact_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    return router
