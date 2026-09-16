from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth import AuthService, ChatPrincipal
from app.db import SafetyAlertRecord
from app.safety import SafetyAlertService
from app.schemas.common import StrictModel

from .auth import ChatSessionGuard


class SafetyAlertView(StrictModel):
    id: UUID
    rule_id: str
    entity_id: str
    message: str
    status: str
    level: int
    created_at: datetime
    expires_at: datetime


class SafetyAlertListResponse(StrictModel):
    items: list[SafetyAlertView]
    total: int


def create_safety_router(
    service: SafetyAlertService, auth_service: AuthService
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/safety", tags=["safety"])
    guard = ChatSessionGuard(auth_service)

    @router.get("/alerts", response_model=SafetyAlertListResponse)
    async def list_alerts(
        principal: Annotated[ChatPrincipal, Depends(guard)],
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
    ) -> SafetyAlertListResponse:
        active = await service._store.escalating_for_user(principal.user_id)
        records: list[SafetyAlertRecord] = active[:limit]
        return SafetyAlertListResponse(
            items=[
                SafetyAlertView(
                    id=record.id,
                    rule_id=record.rule_id,
                    entity_id=record.entity_id,
                    message=record.message,
                    status=record.status,
                    level=record.level,
                    created_at=record.created_at,
                    expires_at=record.expires_at,
                )
                for record in records
            ],
            total=len(records),
        )

    @router.post("/alerts/{alert_id}/ack", response_model=SafetyAlertView)
    async def ack_alert(
        alert_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> SafetyAlertView:
        record = await service._store.get(alert_id)
        if record is None or record.user_id != principal.user_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="alert not found")
        if not await service.acknowledge(alert_id, source="chat_button"):
            raise HTTPException(status.HTTP_409_CONFLICT, detail="alert not ackable")
        fresh = await service._store.get(alert_id)
        assert fresh is not None
        return SafetyAlertView(
            id=fresh.id,
            rule_id=fresh.rule_id,
            entity_id=fresh.entity_id,
            message=fresh.message,
            status=fresh.status,
            level=fresh.level,
            created_at=fresh.created_at,
            expires_at=fresh.expires_at,
        )

    return router
