from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.config import DatabaseConfigStore, HubConfig
from app.db import Base, ConfigPointerRecord, ConfigVersionRecord, Database, create_database
from app.home_assistant import HomeAssistantState
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
        published = await client.put(
            "/api/v1/admin/config/current",
            headers=headers,
            json=candidate,
        )
        versions = await client.get("/api/v1/admin/config/versions", headers=headers)
        page = await client.get("/admin/models")
        persona_page = await client.get("/admin/personas")
        chat_page = await client.get("/chat")
        chat_debug_page = await client.get("/chat/debug")
        pet_page = await client.get("/desktop/pet/")
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
    assert published.status_code == 200
    assert published.json()["config"]["models"]["cloud"]["model"] == "dialogue-v2"
    assert [item["status"] for item in versions.json()] == ["published", "superseded"]
    assert chat_debug_page.status_code == 200
    assert "聊天密码" in chat_debug_page.text
    assert 'id="text-reply-voice"' in chat_debug_page.text
    assert "文字回复播报" in chat_debug_page.text
    assert pet_page.status_code == 200
    assert 'id="live2d"' in pet_page.text
    # With web/apps/admin/dist present the admin routes serve the Vue SPA;
    # source-only checkouts fall back to the vanilla pages.
    spa_mode = '<div id="app"></div>' in page.text
    assert page.status_code == 200
    assert persona_page.status_code == 200
    assert all(response.status_code == 200 for response in module_pages.values())
    if spa_mode:
        assert all('<div id="app"></div>' in r.text for r in module_pages.values())
    else:
        assert "模型与路由" in page.text
        assert "角色与表达" in persona_page.text
        assert all("后台主导航" in r.text for r in module_pages.values())
    assert 'href="#"' not in page.text
    assert 'href="#"' not in persona_page.text
    assert chat_page.status_code == 200
    # /chat serves the Vue build when web/apps/chat/dist exists and falls
    # back to the vanilla debug page in source-only checkouts.
    assert (
        '<div id="app"></div>' in chat_page.text or "文字聊天调试台" in chat_page.text
    )


async def test_admin_model_connection_uses_lm_studio_native_model_list(
    database: Database,
    bootstrap: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_fetch_json(
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> object:
        captured.update(url=url, headers=headers, timeout_seconds=timeout_seconds)
        return {
            "models": [
                {
                    "type": "llm",
                    "key": "local-model",
                    "display_name": "Local Model",
                    "loaded_instances": [{"id": "local-model"}],
                },
                {
                    "type": "llm",
                    "key": "other-model",
                    "display_name": "Other Model",
                    "loaded_instances": [],
                },
            ]
        }

    monkeypatch.setattr("app.api.admin_config._fetch_json", fake_fetch_json)
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
        current = await client.get("/api/v1/admin/config/current", headers=headers)
        endpoint = current.json()["config"]["models"]["local"]
        endpoint["base_url"] = "http://127.0.0.1:1234"
        response = await client.post(
            "/api/v1/admin/config/models/test",
            headers=headers,
            json={"endpoint": endpoint},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["probe_kind"] == "lm_studio_native_v1"
    assert body["target_url"] == "http://127.0.0.1:1234/api/v1/models"
    assert body["model_available"] is True
    assert body["model_loaded"] is True
    assert [item["key"] for item in body["models"]] == ["local-model", "other-model"]
    assert captured["url"] == "http://127.0.0.1:1234/api/v1/models"


async def test_admin_home_assistant_connection_returns_entity_inventory(
    database: Database,
    bootstrap: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch_states(_: object) -> tuple[HomeAssistantState, ...]:
        now = datetime.now(UTC)
        return (
            HomeAssistantState(
                entity_id="light.kitchen",
                state="on",
                attributes={"friendly_name": "厨房灯", "device_class": "light"},
                last_changed=now,
                last_updated=now,
            ),
        )

    async def fake_close(_: object) -> None:
        return None

    monkeypatch.setattr(
        "app.api.admin_config.HomeAssistantClient.fetch_states", fake_fetch_states
    )
    monkeypatch.setattr("app.api.admin_config.HomeAssistantClient.close", fake_close)
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
        response = await client.post(
            "/api/v1/admin/config/integrations/home-assistant/test",
            headers=headers,
            json={
                "config": {
                    "enabled": False,
                    "instance_id": "home-main",
                    "base_url": "https://ha.example.test",
                    "secret_value": "admin-token",
                    "entities": [],
                }
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["message"].endswith("1 个实体")
    assert body["entities"] == [
        {
            "entity_id": "light.kitchen",
            "friendly_name": "厨房灯",
            "domain": "light",
            "state": "on",
            "device_class": "light",
            "unit_of_measurement": None,
        }
    ]


async def test_admin_voice_asr_environment_check_reports_optional_dependency(
    database: Database,
    bootstrap: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DatabaseConfigStore(database, bootstrap)
    app = create_app(
        database,
        config_store=store,
        watch_config=False,
        admin_token="test-admin-token",
    )
    headers = {"Authorization": "Bearer test-admin-token"}
    payload = {
        "asr": {
            "provider": "faster_whisper",
            "model": "small",
            "base_url": None,
            "language": "zh",
            "device": "cpu",
            "compute_type": "int8",
            "runs_local": True,
        }
    }

    monkeypatch.setattr("app.api.admin_config.importlib.util.find_spec", lambda name: None)
    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        missing = await client.post(
            "/api/v1/admin/config/voice/asr/check",
            headers=headers,
            json=payload,
        )

    assert missing.status_code == 200, missing.text
    assert missing.json()["ok"] is False
    assert missing.json()["dependency_available"] is False
    assert missing.json()["model_load_checked"] is False


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
