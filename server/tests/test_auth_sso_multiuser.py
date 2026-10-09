"""多用户 SSO：白名单解析、按 sub 开户/绑定、停用拒绝与回调链路。

ID-01 多人身份解除暂缓后的身份基线：OIDC sub 固定映射 app_user.sso_sub，
存量未绑定业主由 owner_sub 首次登录回填，其余白名单 sub 以 member 开户。
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.auth import create_auth_router
from app.api.auth_sso import SsoSettings, create_sso_router, load_sso_settings
from app.auth import AuthService, InvalidCredentials
from app.db import AppUserRecord, Base, Database, create_database


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    result = create_database("sqlite+aiosqlite:///:memory:")
    async with result.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield result
    finally:
        await result.close()


def test_load_sso_settings_parses_multi_sub_whitelist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIA_SSO_ISSUER", "https://pnkx.top")
    monkeypatch.setenv("ARIA_SSO_CLIENT_ID", "companion-hub")
    monkeypatch.setenv("ARIA_SSO_CLIENT_SECRET", "secret")
    monkeypatch.delenv("ARIA_SSO_ALLOWED_SUBS", raising=False)
    monkeypatch.delenv("ARIA_SSO_ALLOWED_SUB", raising=False)
    assert load_sso_settings().enabled is False

    monkeypatch.setenv("ARIA_SSO_ALLOWED_SUBS", "owner-sub, member-a  member-b")
    settings = load_sso_settings()
    assert settings.enabled is True
    assert settings.allowed_subs == ("owner-sub", "member-a", "member-b")
    # 未显式指定业主时取名单首个
    assert settings.owner_sub == "owner-sub"

    monkeypatch.setenv("ARIA_SSO_OWNER_SUB", "member-b")
    assert load_sso_settings().owner_sub == "member-b"

    # 兼容旧单值变量
    monkeypatch.delenv("ARIA_SSO_ALLOWED_SUBS")
    monkeypatch.delenv("ARIA_SSO_OWNER_SUB")
    monkeypatch.setenv("ARIA_SSO_ALLOWED_SUB", "legacy-sub")
    legacy = load_sso_settings()
    assert legacy.allowed_subs == ("legacy-sub",)
    assert legacy.owner_sub == "legacy-sub"


async def test_login_sso_provisioned_binds_legacy_owner_and_creates_members(
    database: Database,
) -> None:
    service = AuthService(database)
    # 存量部署：本地密码初始化的业主（无 sso_sub）
    legacy = await service.setup(display_name="Legacy Owner", password="correct horse battery")

    owner_login = await service.login_sso_provisioned(
        sub="owner-sub", display_name="业主", owner_sub="owner-sub"
    )
    assert owner_login.principal.role == "owner"
    assert owner_login.principal.user_id == legacy.principal.user_id

    member_login = await service.login_sso_provisioned(
        sub="member-a", display_name="成员A", owner_sub="owner-sub"
    )
    assert member_login.principal.role == "member"
    assert member_login.principal.user_id != legacy.principal.user_id
    assert member_login.principal.display_name == "成员A"

    # 复登映射同一用户并同步 display_name
    renamed = await service.login_sso_provisioned(
        sub="member-a", display_name="成员A改", owner_sub="owner-sub"
    )
    assert renamed.principal.user_id == member_login.principal.user_id
    assert renamed.principal.display_name == "成员A改"

    async with database.sessions() as session:
        users = {
            row.sso_sub: row
            for row in await session.scalars(
                select(AppUserRecord).where(AppUserRecord.sso_sub.is_not(None))
            )
        }
    assert set(users) == {"owner-sub", "member-a"}
    assert users["owner-sub"].role == "owner"
    assert users["member-a"].role == "member"
    assert users["owner-sub"].display_name == "业主"


async def test_login_sso_provisioned_fresh_deploy_owner_sub_creates_owner(
    database: Database,
) -> None:
    service = AuthService(database)
    login = await service.login_sso_provisioned(
        sub="first-sub", display_name="首位", owner_sub="first-sub"
    )
    assert login.principal.role == "owner"
    # display_name 缺省兜底
    second = await service.login_sso_provisioned(
        sub="second-sub", display_name="", owner_sub="first-sub"
    )
    assert second.principal.role == "member"
    assert second.principal.display_name == "用户"


async def test_login_sso_provisioned_rejects_disabled_user(database: Database) -> None:
    service = AuthService(database)
    login = await service.login_sso_provisioned(
        sub="member-a", display_name="成员A", owner_sub="owner-sub"
    )
    async with database.sessions.begin() as session:
        record = await session.get(AppUserRecord, login.principal.user_id)
        assert record is not None
        record.status = "disabled"
    with pytest.raises(InvalidCredentials, match="inactive"):
        await service.login_sso_provisioned(
            sub="member-a", display_name="成员A", owner_sub="owner-sub"
        )


class _StubSSOClient:
    """替身 httpx.AsyncClient：授权码换令牌 + userinfo 返回可配置身份。"""

    sub = "owner-sub"
    name = "业主"

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _StubSSOClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": "stub-access-token"},
            request=httpx.Request("POST", url),
        )

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={"sub": type(self).sub, "name": type(self).name},
            request=httpx.Request("GET", url),
        )


async def test_sso_callback_whitelist_and_provisioning(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _StubSSOClient)
    service = AuthService(database)
    settings = SsoSettings(
        issuer="https://pnkx.top",
        client_id="companion-hub",
        client_secret="secret",
        allowed_subs=("owner-sub", "member-a"),
        owner_sub="owner-sub",
    )
    app = FastAPI()
    app.include_router(create_auth_router(service, admin_token=None))
    app.include_router(create_sso_router(service, settings))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        login = await client.get("/api/v1/auth/sso/login")
        assert login.status_code == 302
        state = parse_qs(urlsplit(login.headers["Location"]).query)["state"][0]

        # 白名单外拒绝
        _StubSSOClient.sub = "stranger"
        stranger = await client.get(
            "/api/v1/auth/sso/callback", params={"code": "c1", "state": state}
        )
        assert stranger.status_code == 200
        assert "无权登录" in stranger.text

        # 白名单内成员开户（state 一次性，重新发起）
        login = await client.get("/api/v1/auth/sso/login")
        state = parse_qs(urlsplit(login.headers["Location"]).query)["state"][0]
        _StubSSOClient.sub = "member-a"
        _StubSSOClient.name = "成员A"
        member_landing = await client.get(
            "/api/v1/auth/sso/callback", params={"code": "c2", "state": state}
        )
        assert "登录成功" in member_landing.text
        member_token = re.search(r"ariaChatToken', '([^']+)'", member_landing.text)
        assert member_token is not None
        me = await client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {member_token.group(1)}"}
        )
        assert me.status_code == 200
        assert me.json()["user"]["role"] == "member"
        assert me.json()["user"]["display_name"] == "成员A"

        # 业主 sub 开户为 owner
        login = await client.get("/api/v1/auth/sso/login")
        state = parse_qs(urlsplit(login.headers["Location"]).query)["state"][0]
        _StubSSOClient.sub = "owner-sub"
        _StubSSOClient.name = "业主"
        owner_landing = await client.get(
            "/api/v1/auth/sso/callback", params={"code": "c3", "state": state}
        )
        owner_token = re.search(r"ariaChatToken', '([^']+)'", owner_landing.text)
        assert owner_token is not None
        me = await client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {owner_token.group(1)}"}
        )
        assert me.json()["user"]["role"] == "owner"


async def test_sso_callback_rejects_replayed_state(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _StubSSOClient)
    service = AuthService(database)
    settings = SsoSettings(
        issuer="https://pnkx.top",
        client_id="companion-hub",
        client_secret="secret",
        allowed_subs=("owner-sub",),
        owner_sub="owner-sub",
    )
    app = FastAPI()
    app.include_router(create_sso_router(service, settings))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        login = await client.get("/api/v1/auth/sso/login")
        state = parse_qs(urlsplit(login.headers["Location"]).query)["state"][0]
        _StubSSOClient.sub = "owner-sub"
        first = await client.get(
            "/api/v1/auth/sso/callback", params={"code": "c1", "state": state}
        )
        replay = await client.get(
            "/api/v1/auth/sso/callback", params={"code": "c2", "state": state}
        )
        assert "登录成功" in first.text
        assert "状态校验失败" in replay.text


async def test_setup_claims_only_owner_for_local_password(database: Database) -> None:
    """本地密码通道只认领业主；SSO member 不被 setup 抢绑凭据。"""
    service = AuthService(database)
    member = await service.login_sso_provisioned(
        sub="member-a", display_name="成员A", owner_sub="owner-sub"
    )
    setup = await service.setup(display_name="离线业主", password="correct horse battery")
    assert setup.principal.role == "owner"
    assert setup.principal.user_id != member.principal.user_id
