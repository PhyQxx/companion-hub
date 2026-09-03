"""Web Push 订阅 API：VAPID 公钥下发、浏览器订阅登记与退订。

注意：不要加 `from __future__ import annotations`——FastAPI 0.141 的懒路由
在延迟求值注解时无法解析 `Depends(guard)`，会把 principal 降级成 query 参数。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import Field

from app.auth import AuthService, ChatPrincipal
from app.config import ConfigStore, DatabaseConfigStore
from app.push import PushSubscriptionStore
from app.schemas.common import StrictModel

from .auth import ChatSessionGuard


class PushSubscriptionKeys(StrictModel):
    p256dh: Annotated[str, Field(min_length=40, max_length=255)]
    auth: Annotated[str, Field(min_length=8, max_length=255)]


class PushSubscribeRequest(StrictModel):
    endpoint: Annotated[str, Field(min_length=16, max_length=768, pattern=r"^https://")]
    keys: PushSubscriptionKeys


class PushUnsubscribeRequest(StrictModel):
    endpoint: Annotated[str, Field(min_length=16, max_length=768, pattern=r"^https://")]


class PushVapidKeyResponse(StrictModel):
    enabled: bool
    public_key: str | None = None


def create_push_router(
    store: PushSubscriptionStore,
    config_store: ConfigStore | DatabaseConfigStore,
    auth_service: AuthService,
) -> APIRouter:
    guard = ChatSessionGuard(auth_service)
    router = APIRouter(prefix="/api/v1/push", tags=["push"])

    @router.get("/vapid-key", response_model=PushVapidKeyResponse)
    async def vapid_key(
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> PushVapidKeyResponse:
        del principal
        push = config_store.current.config.integrations.push
        return PushVapidKeyResponse(
            enabled=push.enabled, public_key=push.vapid_public_key
        )

    @router.post("/subscribe", status_code=status.HTTP_201_CREATED)
    async def subscribe(
        body: PushSubscribeRequest,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> dict[str, str]:
        await store.subscribe(
            user_id=principal.user_id,
            endpoint=body.endpoint,
            p256dh=body.keys.p256dh,
            auth=body.keys.auth,
        )
        return {"endpoint": body.endpoint}

    @router.post("/unsubscribe", status_code=status.HTTP_204_NO_CONTENT)
    async def unsubscribe(
        body: PushUnsubscribeRequest,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> None:
        await store.unsubscribe(user_id=principal.user_id, endpoint=body.endpoint)

    return router
