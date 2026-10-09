"""多用户管理：用户列表、停用/启用与改名（仅业主）。

开户由 SSO 白名单在回调中自动完成（pnkx sub ↔ app_user.sso_sub）；
这里提供业主的运维视角：查看全部用户、停用（同事务撤销其全部会话，
authenticate 依 status 拒绝后续请求）、恢复与改名。角色变更与用户删除
涉及跨域数据血缘（B-02/B-03），不在本批提供。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field
from sqlalchemy import func, select, update

from app.db import AppUserRecord, AuthSessionRecord, Database
from app.schemas.common import StrictModel

from .admin_config import AdminTokenGuard


class AdminUserItem(StrictModel):
    id: UUID
    display_name: str
    role: str
    status: str
    sso_sub: str | None
    created_at: datetime
    active_sessions: int


class AdminUserListResponse(StrictModel):
    items: list[AdminUserItem]
    total: int


class AdminUserUpdate(StrictModel):
    display_name: Annotated[str | None, Field(min_length=1, max_length=160)] = None
    status: Annotated[str | None, Field(pattern="^(active|disabled)$")] = None


def create_admin_users_router(database: Database, *, admin_token: str | None) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/users",
        tags=["admin-users"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    @router.get("", response_model=AdminUserListResponse)
    async def list_users(
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> AdminUserListResponse:
        now = datetime.now(UTC)
        async with database.sessions() as session:
            total = int(
                await session.scalar(select(func.count()).select_from(AppUserRecord)) or 0
            )
            rows = list(
                await session.scalars(
                    select(AppUserRecord).order_by(AppUserRecord.created_at).limit(limit).offset(offset)
                )
            )
            session_counts = {
                row[0]: int(row[1] or 0)
                for row in await session.execute(
                    select(AuthSessionRecord.user_id, func.count())
                    .where(
                        AuthSessionRecord.revoked_at.is_(None),
                        AuthSessionRecord.expires_at > now,
                    )
                    .group_by(AuthSessionRecord.user_id)
                )
            }
        return AdminUserListResponse(
            items=[
                AdminUserItem(
                    id=row.id,
                    display_name=row.display_name,
                    role=row.role,
                    status=row.status,
                    sso_sub=row.sso_sub,
                    created_at=row.created_at,
                    active_sessions=session_counts.get(row.id, 0),
                )
                for row in rows
            ],
            total=total,
        )

    @router.patch("/{user_id}", response_model=AdminUserItem)
    async def update_user(user_id: UUID, body: AdminUserUpdate) -> AdminUserItem:
        now = datetime.now(UTC)
        async with database.sessions.begin() as session:
            record = await session.get(AppUserRecord, user_id)
            if record is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user not found")
            if body.display_name is not None:
                record.display_name = body.display_name
            if body.status is not None and body.status != record.status:
                if body.status == "disabled":
                    active_owners = int(
                        await session.scalar(
                            select(func.count())
                            .select_from(AppUserRecord)
                            .where(
                                AppUserRecord.status == "active",
                                AppUserRecord.role == "owner",
                            )
                        )
                        or 0
                    )
                    if record.role == "owner" and active_owners <= 1:
                        raise HTTPException(
                            status.HTTP_409_CONFLICT,
                            detail="cannot disable the last active owner",
                        )
                    record.status = "disabled"
                    # 停用即撤销全部活跃会话；进行中的请求已过鉴权，按原语义收尾
                    await session.execute(
                        update(AuthSessionRecord)
                        .where(
                            AuthSessionRecord.user_id == user_id,
                            AuthSessionRecord.revoked_at.is_(None),
                            AuthSessionRecord.expires_at > now,
                        )
                        .values(revoked_at=now)
                    )
                else:
                    record.status = "active"
            item = AdminUserItem(
                id=record.id,
                display_name=record.display_name,
                role=record.role,
                status=record.status,
                sso_sub=record.sso_sub,
                created_at=record.created_at,
                active_sessions=0,
            )
        return item

    return router
