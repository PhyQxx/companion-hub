from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field
from sqlalchemy import func, select

from app.db import AppUserRecord, Database, SafetyAlertRecord
from app.safety import SafetyAlertService
from app.schemas.common import StrictModel

from .admin_config import AdminTokenGuard

_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
_EMPTY_UUID = UUID("00000000-0000-0000-0000-000000000000")


class SafetyAlertItem(StrictModel):
    id: UUID
    rule_id: str
    entity_id: str
    severity: str
    message: str
    status: str
    level: int
    l1_at: datetime
    l2_at: datetime | None
    acked_at: datetime | None
    ack_source: str | None
    expires_at: datetime
    created_at: datetime


class SafetyAlertListResponse(StrictModel):
    items: list[SafetyAlertItem]
    total: int
    limit: int
    offset: int


class AuthorizationItem(StrictModel):
    id: UUID
    contact_name: str
    channel: str
    destination: str
    status: str
    created_at: datetime
    revoked_at: datetime | None


class AuthorizationListResponse(StrictModel):
    items: list[AuthorizationItem]
    total: int


class AuthorizationCreate(StrictModel):
    user_id: UUID
    contact_name: Annotated[str, Field(min_length=1, max_length=120)]
    destination: Annotated[str, Field(min_length=3, max_length=254, pattern=_EMAIL_PATTERN)]


class SafetyStatusResponse(StrictModel):
    enabled: bool
    escalation_enabled: bool
    confirm_window_seconds: int
    push_retry_minutes: int
    active_alerts: int
    active_authorizations: int


def _alert_item(record: SafetyAlertRecord) -> SafetyAlertItem:
    return SafetyAlertItem(
        id=record.id,
        rule_id=record.rule_id,
        entity_id=record.entity_id,
        severity=record.severity,
        message=record.message,
        status=record.status,
        level=record.level,
        l1_at=record.l1_at,
        l2_at=record.l2_at,
        acked_at=record.acked_at,
        ack_source=record.ack_source,
        expires_at=record.expires_at,
        created_at=record.created_at,
    )


async def _owner_id(database: Database) -> UUID:
    """单用户部署：告警与授权按首用户聚合；无用户时用空 UUID（不命中任何行）。"""
    async with database.sessions() as session:
        owner = await session.scalar(
            select(AppUserRecord.id).order_by(AppUserRecord.created_at)
        )
    return owner if owner is not None else _EMPTY_UUID


def create_admin_safety_router(
    service: SafetyAlertService, *, admin_token: str | None
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/safety",
        tags=["admin-safety"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )
    # 路由与同一装配的服务共享数据库句柄
    database: Database = service._database

    @router.get("/status", response_model=SafetyStatusResponse)
    async def safety_status() -> SafetyStatusResponse:
        config = service._config_store.current.config.safety
        owner = await _owner_id(database)
        async with database.sessions() as session:
            active_alerts = await session.scalar(
                select(func.count())
                .select_from(SafetyAlertRecord)
                .where(SafetyAlertRecord.status == "escalating")
            )
        authorizations = await service.authorizations.list_for_user(owner)
        active_auth = sum(1 for item in authorizations if item.status == "active")
        return SafetyStatusResponse(
            enabled=config.enabled,
            escalation_enabled=config.escalation_enabled,
            confirm_window_seconds=config.confirm_window_seconds,
            push_retry_minutes=config.push_retry_minutes,
            active_alerts=int(active_alerts or 0),
            active_authorizations=active_auth,
        )

    @router.get("/alerts", response_model=SafetyAlertListResponse)
    async def list_alerts(
        status_filter: Annotated[str | None, Query(alias="status")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> SafetyAlertListResponse:
        query = select(SafetyAlertRecord)
        count_query = select(func.count()).select_from(SafetyAlertRecord)
        if status_filter is not None:
            if status_filter not in {"escalating", "acknowledged", "expired"}:
                raise HTTPException(422, "invalid status filter")
            query = query.where(SafetyAlertRecord.status == status_filter)
            count_query = count_query.where(SafetyAlertRecord.status == status_filter)
        async with database.sessions() as session:
            total = int(await session.scalar(count_query) or 0)
            records: list[SafetyAlertRecord] = list(
                await session.scalars(
                    query.order_by(SafetyAlertRecord.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
        return SafetyAlertListResponse(
            items=[_alert_item(record) for record in records],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.post("/alerts/{alert_id}/ack", response_model=SafetyAlertItem)
    async def ack_alert(alert_id: UUID) -> SafetyAlertItem:
        if not await service.acknowledge(alert_id, source="admin"):
            raise HTTPException(409, "alert not ackable")
        record = await service._store.get(alert_id)
        assert record is not None
        return _alert_item(record)

    @router.get("/authorizations", response_model=AuthorizationListResponse)
    async def list_authorizations() -> AuthorizationListResponse:
        owner = await _owner_id(database)
        records = await service.authorizations.list_for_user(owner)
        return AuthorizationListResponse(
            items=[
                AuthorizationItem(
                    id=item.id,
                    contact_name=item.contact_name,
                    channel=item.channel,
                    destination=item.destination,
                    status=item.status,
                    created_at=item.created_at,
                    revoked_at=item.revoked_at,
                )
                for item in records
            ],
            total=len(records),
        )

    @router.post(
        "/authorizations", response_model=AuthorizationItem, status_code=status.HTTP_201_CREATED
    )
    async def create_authorization(body: AuthorizationCreate) -> AuthorizationItem:
        record = await service.authorizations.create(
            user_id=body.user_id,
            contact_name=body.contact_name,
            destination=body.destination,
        )
        return AuthorizationItem(
            id=record.id,
            contact_name=record.contact_name,
            channel=record.channel,
            destination=record.destination,
            status=record.status,
            created_at=record.created_at,
            revoked_at=record.revoked_at,
        )

    @router.post("/authorizations/{authorization_id}/revoke", response_model=AuthorizationItem)
    async def revoke_authorization(authorization_id: UUID) -> AuthorizationItem:
        record = await service.authorizations.revoke(authorization_id, at=datetime.now(UTC))
        if record is None:
            raise HTTPException(404, "authorization not found")
        return AuthorizationItem(
            id=record.id,
            contact_name=record.contact_name,
            channel=record.channel,
            destination=record.destination,
            status=record.status,
            created_at=record.created_at,
            revoked_at=record.revoked_at,
        )

    return router
