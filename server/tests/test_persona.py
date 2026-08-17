from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from test_database_config import config_yaml

from app.config import DatabaseConfigStore
from app.db import Base, Database, create_database
from app.main import create_app
from app.persona import PersonaConfig, PersonaStore


@pytest.fixture
async def persona_database() -> AsyncIterator[Database]:
    database = create_database("sqlite+aiosqlite:///:memory:")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield database
    finally:
        await database.close()


async def test_persona_store_publishes_and_rolls_back(persona_database: Database) -> None:
    store = PersonaStore(persona_database)
    first = await store.load()
    draft = await store.create_draft(
        PersonaConfig(name="Nova", identity="测试人格"), actor="test"
    )

    published = await store.publish(draft.version)
    restored = await store.rollback(first.version, actor="test")

    assert published.persona.name == "Nova"
    assert restored.persona.name == "Aria"
    assert restored.rollback_from == first.version
    assert [item.status for item in await store.list_versions()] == [
        "published",
        "superseded",
        "superseded",
    ]


async def test_persona_admin_api_manages_versions(
    persona_database: Database, tmp_path
) -> None:
    config_path = tmp_path / "hub.yaml"
    config_path.write_text(config_yaml(), encoding="utf-8")
    config_store = DatabaseConfigStore(persona_database, config_path)
    app = create_app(
        persona_database,
        config_store=config_store,
        watch_config=False,
        admin_token="test-admin-token",
    )
    headers = {"Authorization": "Bearer test-admin-token"}

    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        current = await client.get("/api/v1/admin/personas/current", headers=headers)
        candidate = current.json()["persona"]
        candidate["speaking_style"] = "温暖但不啰嗦"
        draft = await client.post(
            "/api/v1/admin/personas/versions", headers=headers, json=candidate
        )
        published = await client.post(
            f"/api/v1/admin/personas/versions/{draft.json()['version']}/publish",
            headers=headers,
        )

    assert current.status_code == 200
    assert draft.status_code == 201
    assert published.json()["persona"]["speaking_style"] == "温暖但不啰嗦"
