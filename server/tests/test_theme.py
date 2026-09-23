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


class TestScheduledTheme:
    async def test_validate_schedule_boundaries(self) -> None:
        from app.appearance import validate_schedule

        assert validate_schedule("07:00", "19:00") == ("07:00", "19:00")
        with pytest.raises(ValueError, match="HH:MM"):
            validate_schedule("7:00", "19:00")
        with pytest.raises(ValueError, match="HH:MM"):
            validate_schedule("07:00", "24:30")
        with pytest.raises(ValueError, match="不能相同"):
            validate_schedule("08:00", "08:00")

    async def test_schedule_prefers_light_windows(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from app.appearance import schedule_prefers_light

        tz = ZoneInfo("Asia/Shanghai")
        assert schedule_prefers_light("07:00", "19:00", datetime(2026, 9, 22, 12, 0, tzinfo=tz))
        assert not schedule_prefers_light("07:00", "19:00", datetime(2026, 9, 22, 21, 0, tzinfo=tz))
        assert schedule_prefers_light("07:00", "19:00", datetime(2026, 9, 22, 7, 0, tzinfo=tz))
        assert not schedule_prefers_light("07:00", "19:00", datetime(2026, 9, 22, 19, 0, tzinfo=tz))
        # 亮窗跨夜（19:00 起亮、07:00 起暗）的倒置配置
        assert schedule_prefers_light("19:00", "07:00", datetime(2026, 9, 22, 23, 0, tzinfo=tz))
        assert not schedule_prefers_light("19:00", "07:00", datetime(2026, 9, 22, 12, 0, tzinfo=tz))

    async def test_scheduled_preference_resolves_and_persists(self, store: ThemeStore) -> None:
        await store.load_builtin_themes()
        saved = await store.set_preference("scheduled", light_time="07:00", dark_time="19:00")
        assert saved.selection == "scheduled"
        assert saved.appearance_mode == "scheduled"
        assert saved.schedule == ("07:00", "19:00")
        # 生效主题按当前本地时间解析为明暗之一
        assert saved.theme.key in {"pure-light", "midnight-violet"}

        # 重新读取持久化的时段边界
        reread = await store.get_preference()
        assert reread.schedule == ("07:00", "19:00")

        # 切回普通模式清空时段
        plain = await store.set_preference("pure-light")
        assert plain.schedule is None
        cleared = await store.get_preference()
        assert cleared.schedule is None

    async def test_invalid_schedule_rejected(self, store: ThemeStore) -> None:
        await store.load_builtin_themes()
        with pytest.raises(ValueError, match="不能相同"):
            await store.set_preference("scheduled", light_time="09:00", dark_time="09:00")

    async def test_admin_api_roundtrips_schedule(self, store: ThemeStore) -> None:
        await store.load_builtin_themes()
        app = FastAPI()
        app.include_router(create_admin_theme_router(store, admin_token="test-admin-token"))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            updated = await client.put(
                "/api/v1/admin/ui/preferences",
                headers={"Authorization": "Bearer test-admin-token"},
                json={"selection": "scheduled", "light_time": "06:30", "dark_time": "18:30"},
            )
            assert updated.status_code == 200
            body = updated.json()
            assert body["selection"] == "scheduled"
            assert body["schedule"] == {"light_time": "06:30", "dark_time": "18:30"}

            invalid = await client.put(
                "/api/v1/admin/ui/preferences",
                headers={"Authorization": "Bearer test-admin-token"},
                json={"selection": "scheduled", "light_time": "99:00", "dark_time": "19:00"},
            )
            assert invalid.status_code == 422

            # 切回非定时选择时 schedule 为 null
            plain = await client.put(
                "/api/v1/admin/ui/preferences",
                headers={"Authorization": "Bearer test-admin-token"},
                json={"selection": "system"},
            )
            assert plain.json()["schedule"] is None
