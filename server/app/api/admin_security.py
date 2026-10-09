from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import AuthService, InvalidCredentials
from app.schemas.common import StrictModel

from .admin_config import AdminTokenGuard, set_runtime_admin_token


class ChangeAdminTokenRequest(StrictModel):
    new_token: str


class ResetChatPasswordRequest(StrictModel):
    new_password: str


def create_admin_security_router(
    *,
    admin_token: str | None,
    auth_service: AuthService | None,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/system",
        tags=["admin-security"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    @router.post("/admin-token")
    async def change_admin_token(body: ChangeAdminTokenRequest) -> dict[str, str]:
        """修改管理后台访问令牌，修改后即时生效。"""
        if not body.new_token or len(body.new_token) < 8:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="admin token must be at least 8 characters",
            )
        set_runtime_admin_token(body.new_token)
        return {"message": "admin token updated"}

    @router.post("/chat-password")
    async def reset_chat_password(body: ResetChatPasswordRequest) -> dict[str, str]:
        """重置业主本地密码（离线降级通道）并撤销所有活跃会话。

        多用户下本地密码凭据仅业主持有（setup_slot=1 单槽）；成员账号
        由 pnkx SSO 管理，不经此接口。
        """
        if auth_service is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="auth service is not available",
            )
        if len(body.new_password) < 8:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="password must be at least 8 characters",
            )
        try:
            await auth_service.reset_password(body.new_password)
        except InvalidCredentials as error:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=str(error),
            ) from error
        return {"message": "chat password reset, all sessions revoked"}

    return router
