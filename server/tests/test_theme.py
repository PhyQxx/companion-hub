from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.theme import create_admin_theme_router, create_theme_router
from app.appearance import ThemeStore
from app.auth import AuthService
from app.db import Base, Database, create_database


@pytest.fixture
def tmp_db(tmp_path: Any) -> Database:
    return create_database(f"sqlite+aiosqlite:///{tmp_path / 'test_theme.db'}")


@pytest.fixture
async def database(tmp_db: Database) -> Database:
    async with tmp_db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return tmp_db


@pytest.fixture
def store(database: Database) -> ThemeStore:
    return ThemeStore(database)


class TestThemeStore:
    async def test_loads_builtin_themes_idempotently(self, store: ThemeStore) -> None:
        assert await store.load_builtin_themes() == 2
        assert await store.load_builtin_themes() == 0
        themes = await store.list_themes()
        assert {theme.key for theme in themes} == {"pure-light", "midnight-violet"}

    async def test_default_and_updated_preference(self, store: ThemeStore) -> None:
        await store.load_builtin_themes()
        initial = await store.get_preference()
        assert initial.selection == "pure-light"
        assert initial.updated_at is None

        updated = await store.set_preference("system")
        assert updated.selection == "system"
        assert updated.appearance_mode == "system"
        assert updated.theme.key == "pure-light"
        assert updated.updated_at is not None

        dark = await store.set_preference("midnight-violet")
        assert dark.selection == "midnight-violet"
        assert dark.appearance_mode == "dark"
        assert dark.theme.key == "midnight-violet"


async def test_admin_theme_api_lists_and_updates_preference(store: ThemeStore) -> None:
    await store.load_builtin_themes()
    app = FastAPI()
    app.include_router(create_admin_theme_router(store, admin_token="test-admin-token"))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/api/v1/admin/ui/themes")
        assert denied.status_code == 401

        headers = {"Authorization": "Bearer test-admin-token"}
        themes = await client.get("/api/v1/admin/ui/themes", headers=headers)
        assert themes.status_code == 200
        assert {item["key"] for item in themes.json()} == {"pure-light", "midnight-violet"}

        updated = await client.put(
            "/api/v1/admin/ui/preferences",
            headers=headers,
            json={"selection": "midnight-violet"},
        )
        assert updated.status_code == 200
        assert updated.json()["selection"] == "midnight-violet"

        invalid = await client.put(
            "/api/v1/admin/ui/preferences",
            headers=headers,
            json={"selection": "unknown"},
        )
        assert invalid.status_code == 422


async def test_chat_theme_api_requires_session_and_updates_shared_preference(
    database: Database,
    store: ThemeStore,
) -> None:
    await store.load_builtin_themes()
    auth = AuthService(database)
    auth_session = await auth.setup(
        display_name="Owner",
        password="correct horse battery staple",
    )
    app = FastAPI()
    app.include_router(create_theme_router(store, auth))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/api/v1/ui/preferences")
        assert denied.status_code == 401

        headers = {"Authorization": f"Bearer {auth_session.access_token}"}
        updated = await client.put(
            "/api/v1/ui/preferences",
            headers=headers,
            json={"selection": "system"},
        )
        assert updated.status_code == 200
        assert updated.json()["selection"] == "system"

        fetched = await client.get("/api/v1/ui/preferences", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json()["appearance_mode"] == "system"
