"""多用户管理 API（仅业主）：列表、停用撤销会话、恢复与改名。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.admin_users import create_admin_users_router
from app.auth import AuthService, InvalidSession
from app.db import Base, Database, create_database


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    result = create_database("sqlite+aiosqlite:///:memory:")
    async with result.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield result
    finally:
        await result.close()


def _app(database: Database) -> FastAPI:
    app = FastAPI()
    app.state.auth_service = AuthService(database)
    app.include_router(create_admin_users_router(database, admin_token="admin-token"))
    return app


async def test_list_disable_enable_and_rename(database: Database) -> None:
    service = AuthService(database)
    owner = await service.login_sso_provisioned(
        sub="owner-sub", display_name="业主", owner_sub="owner-sub"
    )
    member = await service.login_sso_provisioned(
        sub="member-a", display_name="成员A", owner_sub="owner-sub"
    )
    app = _app(database)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {owner.access_token}"}
        listing = await client.get("/api/v1/admin/users", headers=headers)
        assert listing.status_code == 200
        items = {item["display_name"]: item for item in listing.json()["items"]}
        assert items["业主"]["role"] == "owner" and items["业主"]["sso_sub"] == "owner-sub"
        assert items["成员A"]["role"] == "member"
        assert items["业主"]["active_sessions"] >= 1

        # 成员会话访问用户管理 → 403（owner-only 路由）
        member_headers = {"Authorization": f"Bearer {member.access_token}"}
        denied = await client.get("/api/v1/admin/users", headers=member_headers)
        assert denied.status_code == 403

        # 停用成员：会话立即失效
        disabled = await client.patch(
            f"/api/v1/admin/users/{member.principal.user_id}",
            headers=headers,
            json={"status": "disabled"},
        )
        assert disabled.status_code == 200
        assert disabled.json()["status"] == "disabled"
        with pytest.raises(InvalidSession):
            await service.authenticate(member.access_token)
        # SSO 复登被拒
        with pytest.raises(Exception, match="inactive"):
            await service.login_sso_provisioned(
                sub="member-a", display_name="成员A", owner_sub="owner-sub"
            )

        # 恢复 + 改名
        enabled = await client.patch(
            f"/api/v1/admin/users/{member.principal.user_id}",
            headers=headers,
            json={"status": "active", "display_name": "成员A改名"},
        )
        assert enabled.json()["status"] == "active"
        assert enabled.json()["display_name"] == "成员A改名"
        relogin = await service.login_sso_provisioned(
            sub="member-a", display_name="成员A改名", owner_sub="owner-sub"
        )
        assert relogin.principal.display_name == "成员A改名"

        # 最后一个活跃业主不能停用
        guard = await client.patch(
            f"/api/v1/admin/users/{owner.principal.user_id}",
            headers=headers,
            json={"status": "disabled"},
        )
        assert guard.status_code == 409

        # admin token 机器访问放行（运维通道）
        machine = await client.get(
            "/api/v1/admin/users", headers={"Authorization": "Bearer admin-token"}
        )
        assert machine.status_code == 200

        # 未知用户 404
        missing = await client.patch(
            "/api/v1/admin/users/00000000-0000-0000-0000-000000000000",
            headers=headers,
            json={"display_name": "不存在"},
        )
        assert missing.status_code == 404
