"""用户名+密码登录：自助设置密码、本地账号管理（MU-05）。

pnkx SSO 之外的第二登录通道：app_user.username（小写唯一）+ 按用户
存放的密码凭据；成员登录后可自助设置/修改密码，业主可在用户管理中
创建本地用户、设置用户名、重置密码。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.admin_users import create_admin_users_router
from app.api.auth import create_auth_router
from app.auth import AuthService
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
    app.include_router(create_auth_router(app.state.auth_service, admin_token=None))
    app.include_router(create_admin_users_router(database, admin_token="admin-token"))
    return app


async def _member(database: Database) -> tuple[str, UUID]:
    service = AuthService(database)
    login = await service.login_sso_provisioned(
        sub="member-a", display_name="成员A", owner_sub="owner-sub"
    )
    return login.access_token, login.principal.user_id


async def test_member_sets_own_password_then_logs_in_with_username(database: Database) -> None:
    member_token, member_user = await _member(database)
    app = _app(database)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {member_token}"}

        # 业主给成员设置用户名
        set_username = await client.patch(
            f"/api/v1/admin/users/{member_user}",
            headers={"Authorization": "Bearer admin-token"},
            json={"username": "Member-A"},
        )
        assert set_username.status_code == 200
        assert set_username.json()["username"] == "member-a"

        # 成员首次设置密码（无当前密码）
        set_password = await client.post(
            "/api/v1/auth/password",
            headers=headers,
            json={"new_password": "member secret 1"},
        )
        assert set_password.status_code == 204

        # 用户名（大写输入归一）+ 密码登录
        login = await client.post(
            "/api/v1/auth/login",
            json={"username": "MEMBER-A", "password": "member secret 1"},
        )
        assert login.status_code == 200
        assert login.json()["user"]["username"] == "member-a"
        assert login.json()["user"]["role"] == "member"

        # 错误密码 / 未知用户名 → 401
        wrong = await client.post(
            "/api/v1/auth/login", json={"username": "member-a", "password": "wrong"}
        )
        unknown = await client.post(
            "/api/v1/auth/login", json={"username": "nobody", "password": "whatever-1"}
        )
        assert wrong.status_code == 401 and unknown.status_code == 401

        # 已有密码后修改必须提供当前密码
        no_current = await client.post(
            "/api/v1/auth/password", headers=headers, json={"new_password": "new secret 2"}
        )
        assert no_current.status_code == 403
        bad_current = await client.post(
            "/api/v1/auth/password",
            headers=headers,
            json={"new_password": "new secret 2", "current_password": "wrong"},
        )
        assert bad_current.status_code == 403
        ok_current = await client.post(
            "/api/v1/auth/password",
            headers=headers,
            json={"new_password": "new secret 2", "current_password": "member secret 1"},
        )
        assert ok_current.status_code == 204
        relogin = await client.post(
            "/api/v1/auth/login", json={"username": "member-a", "password": "new secret 2"}
        )
        assert relogin.status_code == 200
        old_password = await client.post(
            "/api/v1/auth/login", json={"username": "member-a", "password": "member secret 1"}
        )
        assert old_password.status_code == 401


async def test_admin_creates_local_user_and_resets_password(database: Database) -> None:
    await _member(database)  # 确保 owner 之外还有一个成员上下文
    app = _app(database)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin = {"Authorization": "Bearer admin-token"}
        created = await client.post(
            "/api/v1/admin/users",
            headers=admin,
            json={
                "display_name": "本地成员",
                "username": "local.user-1",
                "password": "local pass 1",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["username"] == "local.user-1"
        assert created.json()["role"] == "member"

        # 用户名占用冲突 / 非法用户名（大小写归一后同算占用）
        duplicate = await client.post(
            "/api/v1/admin/users",
            headers=admin,
            json={
                "display_name": "重复",
                "username": "LOCAL.USER-1",
                "password": "local pass 1",
            },
        )
        assert duplicate.status_code == 409
        invalid = await client.post(
            "/api/v1/admin/users",
            headers=admin,
            json={"display_name": "非法", "username": "x", "password": "local pass 1"},
        )
        assert invalid.status_code == 422

        # 本地用户直接用账号密码登录
        login = await client.post(
            "/api/v1/auth/login", json={"username": "local.user-1", "password": "local pass 1"}
        )
        assert login.status_code == 200
        assert login.json()["user"]["display_name"] == "本地成员"

        # 业主重置密码：旧失效、新可用
        reset = await client.post(
            f"/api/v1/admin/users/{created.json()['id']}/password",
            headers=admin,
            json={"new_password": "reset pass 2"},
        )
        assert reset.status_code == 204
        assert (
            await client.post(
                "/api/v1/auth/login", json={"username": "local.user-1", "password": "local pass 1"}
            )
        ).status_code == 401
        assert (
            await client.post(
                "/api/v1/auth/login", json={"username": "local.user-1", "password": "reset pass 2"}
            )
        ).status_code == 200


async def test_legacy_password_only_login_still_works(database: Database) -> None:
    """username 缺省保持旧单密码语义（存量部署/离线业主通道）。"""
    service = AuthService(database)
    setup = await service.setup(display_name="业主", password="owner legacy pass")
    app = _app(database)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/v1/auth/login", json={"password": "owner legacy pass"})
        assert login.status_code == 200
        assert login.json()["user"]["role"] == "owner"
        assert login.json()["user"]["id"] == str(setup.principal.user_id)
