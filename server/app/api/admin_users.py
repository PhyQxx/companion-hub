"""多用户管理：用户列表、停用/启用、改名与账号密码（仅业主）。

SSO 白名单用户由回调自动开户（pnkx sub ↔ app_user.sso_sub）；本地账号
（用户名+密码，无 pnkx 绑定）由业主在这里创建。停用同事务撤销其全部
会话（authenticate 依 status 拒绝后续请求）。角色变更与用户删除涉及
跨域数据血缘（B-02/B-03），不在本批提供。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import Field
from sqlalchemy import func, select, update

from app.auth import AuthService, InvalidCredentials, InvalidUsername
from app.db import AppUserRecord, AuthSessionRecord, Database
from app.schemas.common import StrictModel

from .admin_config import AdminTokenGuard


class AdminUserItem(StrictModel):
    id: UUID
    display_name: str
    role: str
    status: str
    sso_sub: str | None
    username: str | None
    created_at: datetime
    active_sessions: int


class AdminUserListResponse(StrictModel):
    items: list[AdminUserItem]
    total: int


class AdminUserCreate(StrictModel):
    display_name: Annotated[str, Field(min_length=1, max_length=160)]
    username: Annotated[str, Field(min_length=2, max_length=64)]
    password: Annotated[str, Field(min_length=8, max_length=256)]


class AdminUserUpdate(StrictModel):
    display_name: Annotated[str | None, Field(min_length=1, max_length=160)] = None
    status: Annotated[str | None, Field(pattern="^(active|disabled)$")] = None
    username: Annotated[str | None, Field(min_length=2, max_length=64)] = None


class AdminPasswordReset(StrictModel):
    new_password: Annotated[str, Field(min_length=8, max_length=256)]


def _auth_service(request: Request) -> AuthService:
    """运行时组合根把 AuthService 放在 app.state；数据路由装配早于其创建，
    这里按请求晚绑定取用（与 AdminTokenGuard 会话回退同一机制）。"""
    service: AuthService | None = getattr(request.app.state, "auth_service", None)
    if not isinstance(service, AuthService):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="auth service is not available"
        )
    return service


def create_admin_users_router(database: Database, *, admin_token: str | None) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/users",
        tags=["admin-users"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    def _item(record: AppUserRecord, active_sessions: int = 0) -> AdminUserItem:
        return AdminUserItem(
            id=record.id,
            display_name=record.display_name,
            role=record.role,
            status=record.status,
            sso_sub=record.sso_sub,
            username=record.username,
            created_at=record.created_at,
            active_sessions=active_sessions,
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
                    select(AppUserRecord)
                    .order_by(AppUserRecord.created_at)
                    .limit(limit)
                    .offset(offset)
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
            items=[_item(row, session_counts.get(row.id, 0)) for row in rows],
            total=total,
        )

    @router.post("", response_model=AdminUserItem, status_code=status.HTTP_201_CREATED)
    async def create_user(body: AdminUserCreate, request: Request) -> AdminUserItem:
        """创建本地成员（用户名+密码登录，无 pnkx 绑定）。"""
        try:
            user_id = await _auth_service(request).create_local_user(
                display_name=body.display_name,
                username=body.username,
                password=body.password,
            )
        except InvalidUsername as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
        except InvalidCredentials as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        async with database.sessions() as session:
            record = await session.get(AppUserRecord, user_id)
        assert record is not None
        return _item(record)

    @router.patch("/{user_id}", response_model=AdminUserItem)
    async def update_user(
        user_id: UUID, body: AdminUserUpdate, request: Request
    ) -> AdminUserItem:
        # 用户名经独立事务先行（避免与主事务同行锁竞争）；失败即整体未应用
        if body.username is not None:
            try:
                await _auth_service(request).set_username(user_id, body.username)
            except InvalidUsername as error:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
                ) from error
            except InvalidCredentials as error:
                raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
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
                            AuthSessionRecord.expires_at > datetime.now(UTC),
                        )
                        .values(revoked_at=datetime.now(UTC))
                    )
                else:
                    record.status = "active"
            item = _item(record)
        return item

    @router.post(
        "/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT
    )
    async def reset_password(
        user_id: UUID, body: AdminPasswordReset, request: Request
    ) -> None:
        """业主重置指定用户的密码（无须当前密码；该用户既有会话保持有效，
        如需立即失效配合「停用→启用」或让其自助改密后重新登录）。"""
        try:
            await _auth_service(request).set_password(
                user_id, body.new_password, require_current=False
            )
        except InvalidCredentials as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    return router
