from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api import create_auth_router
from app.auth import AuthService, InvalidSession
from app.db import AuthCredentialRecord, AuthSessionRecord, Base, Database, create_database


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    result = create_database("sqlite+aiosqlite:///:memory:")
    async with result.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield result
    finally:
        await result.close()


async def test_setup_login_and_logout_use_hashed_secrets(database: Database) -> None:
    service = AuthService(database)
    app = FastAPI()
    app.include_router(create_auth_router(service, admin_token="admin-token"))
    admin_headers = {"Authorization": "Bearer admin-token"}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        before = await client.get("/api/v1/auth/status")
        unauthorized_setup = await client.post(
            "/api/v1/auth/setup",
            json={"display_name": "Owner", "password": "correct horse battery staple"},
        )
        setup = await client.post(
            "/api/v1/auth/setup",
            headers=admin_headers,
            json={"display_name": "Owner", "password": "correct horse battery staple"},
        )
        repeated = await client.post(
            "/api/v1/auth/setup",
            headers=admin_headers,
            json={"display_name": "Other", "password": "another secure password"},
        )
        token = setup.json()["access_token"]
        chat_headers = {"Authorization": f"Bearer {token}"}
        me = await client.get("/api/v1/auth/me", headers=chat_headers)
        logout = await client.post("/api/v1/auth/logout", headers=chat_headers)
        revoked = await client.get("/api/v1/auth/me", headers=chat_headers)
        wrong = await client.post(
            "/api/v1/auth/login", json={"password": "wrong password"}
        )
        login = await client.post(
            "/api/v1/auth/login",
            json={"password": "correct horse battery staple"},
        )
        after = await client.get("/api/v1/auth/status")

    assert before.json() == {"setup_required": True}
    assert unauthorized_setup.status_code == 401
    assert setup.status_code == 201
    assert setup.json()["user"]["display_name"] == "Owner"
    assert repeated.status_code == 409
    assert me.status_code == 200 and me.json()["user"]["id"] == setup.json()["user"]["id"]
    assert logout.status_code == 204
    assert revoked.status_code == 401
    assert wrong.status_code == 401
    assert login.status_code == 200 and login.json()["access_token"] != token
    assert after.json() == {"setup_required": False}

    async with database.sessions() as session:
        credential = await session.scalar(select(AuthCredentialRecord))
        sessions = list(await session.scalars(select(AuthSessionRecord)))
    assert credential is not None
    assert credential.secret_hash is not None
    assert "correct horse battery staple" not in credential.secret_hash
    assert all(record.access_hash != token for record in sessions)
    assert all(len(record.access_hash) == 64 for record in sessions)


async def test_login_is_rate_limited_after_repeated_failures(database: Database) -> None:
    service = AuthService(database)
    await service.setup(display_name="Owner", password="correct horse battery staple")
    app = FastAPI()
    app.include_router(create_auth_router(service, admin_token="admin-token"))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = [
            await client.post("/api/v1/auth/login", json={"password": "wrong"})
            for _ in range(6)
        ]

    assert [response.status_code for response in responses] == [401, 401, 401, 401, 401, 429]
    assert responses[-1].headers["retry-after"] == "300"


async def test_expired_session_is_rejected(database: Database) -> None:
    service = AuthService(database)
    auth_session = await service.setup(
        display_name="Owner", password="correct horse battery staple"
    )
    async with database.sessions.begin() as session:
        record = await session.get(AuthSessionRecord, auth_session.principal.session_id)
        assert record is not None
        record.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises(InvalidSession, match="invalid chat session"):
        await service.authenticate(auth_session.access_token)
