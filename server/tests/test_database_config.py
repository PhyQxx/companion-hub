from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.config import DatabaseConfigStore, HubConfig
from app.db import Base, ConfigPointerRecord, ConfigVersionRecord, Database, create_database
from app.main import create_app


def config_yaml(model: str = "dialogue-v1") -> str:
    return f"""
schema_version: 1
models:
  cloud:
    provider: openai_compatible
    model: {model}
    base_url: https://models.example/v1
    secret_ref: env:MODEL_API_KEY
    runs_local: false
    max_privacy_level: L1
    max_context_tokens: 32768
    input_cost_per_million: 0
    output_cost_per_million: 0
  local:
    provider: openai_compatible
    model: local-model
    base_url: http://127.0.0.1:11434/v1
    runs_local: true
    max_privacy_level: L2
    max_context_tokens: 32768
    input_cost_per_million: 0
    output_cost_per_million: 0
routes:
  dialogue: {{primary: cloud}}
  utility: {{primary: cloud}}
  private: {{primary: local}}
"""


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    result = create_database("sqlite+aiosqlite:///:memory:")
    async with result.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield result
    finally:
        await result.close()


@pytest.fixture
def bootstrap(tmp_path: Path) -> Path:
    path = tmp_path / "hub.yaml"
    path.write_text(config_yaml(), encoding="utf-8")
    return path


async def test_database_store_bootstraps_publishes_and_rolls_back(
    database: Database,
    bootstrap: Path,
) -> None:
    store = DatabaseConfigStore(database, bootstrap)
    first = await store.load()
    candidate = HubConfig.model_validate(
        {
            **first.config.model_dump(mode="python"),
            "models": {
                **first.config.model_dump(mode="python")["models"],
                "cloud": {
                    **first.config.models["cloud"].model_dump(mode="python"),
                    "model": "dialogue-v2",
                },
            },
        }
    )
    draft = await store.create_draft(candidate, actor="test-admin")

    assert first.version == 1
    assert draft.version == 2
    assert draft.status == "draft"
    assert store.current.version == 1
    with pytest.raises(ValueError, match="draft cannot"):
        await store.rollback(draft.version, actor="test-admin")

    published = await store.publish(draft.version, actor="test-admin")
    assert published.version == 2
    assert published.config.models["cloud"].model == "dialogue-v2"

    restored = await store.rollback(first.version, actor="test-admin")
    assert restored.version == 3
    assert restored.rollback_from == 1
    assert restored.config.models["cloud"].model == "dialogue-v1"

    async with database.sessions() as session:
        pointer = await session.get(ConfigPointerRecord, 1)
        count = await session.scalar(select(func.count()).select_from(ConfigVersionRecord))
        records = list(
            await session.scalars(select(ConfigVersionRecord).order_by(ConfigVersionRecord.id))
        )
    assert pointer is not None and pointer.current_version_id == 3
    assert count == 3
    assert [record.status for record in records] == ["superseded", "superseded", "published"]
    assert records[2].created_by == "test-admin"


async def test_admin_api_requires_token_and_manages_drafts(
    database: Database,
    bootstrap: Path,
) -> None:
    store = DatabaseConfigStore(database, bootstrap)
    app = create_app(
        database,
        config_store=store,
        watch_config=False,
        admin_token="test-admin-token",
    )
    headers = {"Authorization": "Bearer test-admin-token"}

    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        unauthorized = await client.get("/api/v1/admin/config/current")
        current = await client.get("/api/v1/admin/config/current", headers=headers)
        assert current.status_code == 200, current.text
        candidate = current.json()["config"]
        candidate["models"]["cloud"]["model"] = "dialogue-v2"
        draft = await client.post(
            "/api/v1/admin/config/versions",
            headers=headers,
            json=candidate,
        )
        published = await client.post(
            f"/api/v1/admin/config/versions/{draft.json()['version']}/publish",
            headers=headers,
        )
        versions = await client.get("/api/v1/admin/config/versions", headers=headers)
        page = await client.get("/admin/models")
        persona_page = await client.get("/admin/personas")
        chat_page = await client.get("/chat")
        module_pages = {
            path: await client.get(path)
            for path in (
                "/admin",
                "/admin/memory",
                "/admin/devices",
                "/admin/logs",
                "/admin/privacy",
                "/admin/settings",
            )
        }

    assert unauthorized.status_code == 401
    assert current.status_code == 200
    assert draft.status_code == 201 and draft.json()["status"] == "draft"
    assert published.status_code == 200
    assert published.json()["config"]["models"]["cloud"]["model"] == "dialogue-v2"
    assert [item["status"] for item in versions.json()] == ["published", "superseded"]
    assert page.status_code == 200
    assert "模型与路由" in page.text
    assert persona_page.status_code == 200
    assert "角色与表达" in persona_page.text
    assert all(response.status_code == 200 for response in module_pages.values())
    assert all("后台主导航" in response.text for response in module_pages.values())
    assert 'href="#"' not in page.text
    assert 'href="#"' not in persona_page.text
    assert chat_page.status_code == 200
    assert "文字聊天调试台" in chat_page.text


async def test_failed_publish_keeps_database_pointer_and_draft_state(
    database: Database,
    bootstrap: Path,
) -> None:
    reject_publish = False

    async def validator(config: HubConfig) -> None:
        del config
        if reject_publish:
            raise RuntimeError("synthetic_connectivity_failure")

    store = DatabaseConfigStore(database, bootstrap, validators=(validator,))
    first = await store.load()
    candidate_data = first.config.model_dump(mode="python")
    candidate_data["models"]["cloud"]["model"] = "dialogue-v2"
    draft = await store.create_draft(HubConfig.model_validate(candidate_data), actor="admin")
    reject_publish = True

    with pytest.raises(RuntimeError, match="synthetic_connectivity_failure"):
        await store.publish(draft.version, actor="admin")

    assert store.current.version == first.version
    assert (await store.get_version(draft.version)).status == "draft"
    async with database.sessions() as session:
        pointer = await session.get(ConfigPointerRecord, 1)
    assert pointer is not None and pointer.current_version_id == first.version
    assert store.last_error == "RuntimeError"


async def test_admin_api_is_disabled_without_server_token(
    database: Database,
    bootstrap: Path,
) -> None:
    store = DatabaseConfigStore(database, bootstrap)
    app = create_app(database, config_store=store, watch_config=False, admin_token="")
    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/admin/config/current",
            headers={"Authorization": "Bearer anything"},
        )
    assert response.status_code == 503, response.text
