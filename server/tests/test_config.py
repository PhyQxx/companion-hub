from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import ConfigStore, ConfigWatcher
from app.config.models import HubConfig
from app.config.store import load_config_file
from app.llm import LLMRoute
from app.main import create_app


def yaml_config(*, dialogue_model: str = "dialogue-v1", private_local: bool = True) -> str:
    return f"""
schema_version: 1
models:
  cloud:
    provider: openai_compatible
    model: {dialogue_model}
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
    runs_local: {str(private_local).lower()}
    max_privacy_level: L2
    max_context_tokens: 32768
    input_cost_per_million: 0
    output_cost_per_million: 0
routes:
  dialogue: {{primary: cloud}}
  utility: {{primary: cloud}}
  private: {{primary: local}}
"""


async def test_invalid_reload_keeps_last_known_good_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "hub.yaml"
    path.write_text(yaml_config(), encoding="utf-8")
    store = ConfigStore(path)
    first = await store.load()

    path.write_text("schema_version: 99\n", encoding="utf-8")
    with pytest.raises(ValueError):
        await store.reload()

    assert store.current is first
    assert store.last_error is not None
    assert store.audit[-1].success is False


async def test_publish_and_rollback_are_versioned(tmp_path: Path) -> None:
    path = tmp_path / "hub.yaml"
    path.write_text(yaml_config(), encoding="utf-8")
    store = ConfigStore(path)
    first = await store.load()
    path.write_text(yaml_config(dialogue_model="dialogue-v2"), encoding="utf-8")
    second = await store.reload()
    rolled_back = await store.rollback(first.version)

    assert (first.version, second.version, rolled_back.version) == (1, 2, 3)
    assert rolled_back.config.models["cloud"].model == "dialogue-v1"
    assert rolled_back.rollback_from == 2
    assert rolled_back.content_hash == first.content_hash


async def test_async_validator_rejection_is_atomic(tmp_path: Path) -> None:
    path = tmp_path / "hub.yaml"
    path.write_text(yaml_config(), encoding="utf-8")

    async def reject_v2(config: HubConfig) -> None:
        if "dialogue-v2" in repr(config):
            raise RuntimeError("probe_failed")

    store = ConfigStore(path, validators=(reject_v2,))
    first = await store.load()
    path.write_text(yaml_config(dialogue_model="dialogue-v2"), encoding="utf-8")
    with pytest.raises(RuntimeError, match="probe_failed"):
        await store.reload()
    assert store.current is first


async def test_watcher_applies_valid_change_and_survives_invalid_one(tmp_path: Path) -> None:
    path = tmp_path / "hub.yaml"
    path.write_text(yaml_config(), encoding="utf-8")
    store = ConfigStore(path)
    await store.load()
    watcher = ConfigWatcher(store, poll_seconds=0.01)
    await watcher.start()
    try:
        path.write_text(yaml_config(dialogue_model="dialogue-v2"), encoding="utf-8")
        for _ in range(50):
            if store.current.version == 2:
                break
            await asyncio.sleep(0.01)
        assert store.current.config.models["cloud"].model == "dialogue-v2"

        path.write_text("invalid: true\n", encoding="utf-8")
        for _ in range(50):
            if store.last_error is not None:
                break
            await asyncio.sleep(0.01)
        assert store.current.version == 2
        assert store.last_error is not None
    finally:
        await watcher.stop()


async def test_private_route_cannot_publish_cloud_model(tmp_path: Path) -> None:
    path = tmp_path / "hub.yaml"
    path.write_text(yaml_config(private_local=False), encoding="utf-8")
    with pytest.raises(ValueError, match="private route cannot reference cloud"):
        await ConfigStore(path).load()


async def test_optional_voice_route_is_accepted_without_changing_required_routes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "hub.yaml"
    configured = yaml_config().replace(
        "  utility: {primary: cloud}",
        "  voice: {primary: cloud}\n  utility: {primary: cloud}",
    )
    path.write_text(configured, encoding="utf-8")

    snapshot = await ConfigStore(path).load()

    assert snapshot.config.routes[LLMRoute.VOICE].primary == "cloud"


async def test_app_exposes_payload_free_config_metadata(tmp_path: Path) -> None:
    path = tmp_path / "hub.yaml"
    path.write_text(yaml_config(), encoding="utf-8")
    app = create_app(config_store=ConfigStore(path), watch_config=False)

    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/meta/config")
        health = await client.get("/healthz")

    body = response.json()
    assert response.status_code == 200
    assert body["configured"] is True
    assert body["routes"]["dialogue"]["primary"] == "cloud"
    assert "secret_ref" not in response.text
    assert "MODEL_API_KEY" not in response.text
    assert health.json()["configuration"]["version"] == 1


def test_example_config_selects_latest_free_glm_models_by_capability() -> None:
    root = Path(__file__).resolve().parents[2]
    config, _ = load_config_file(root / "config/hub.example.yaml")

    assert config.models["zhipu_text_free"].model == "glm-4.7-flash"
    assert config.models["zhipu_text_free"].kind == "text"
    assert config.models["zhipu_vision_free"].model == "glm-4v-flash"
    assert config.models["zhipu_vision_free"].kind == "vision"
    assert config.models["zhipu_vision_free"].max_tokens == 1024
    assert config.models["zhipu_image_free"].model == "cogview-3-flash"
    assert config.models["zhipu_image_free"].kind == "image_generation"
    assert config.models["zhipu_video_free"].model == "cogvideox-flash"
    assert config.models["zhipu_video_free"].kind == "video_generation"
    assert config.capability_models.vision == "zhipu_vision_free"
    assert config.capability_models.image_generation == "zhipu_image_free"
    assert config.capability_models.video_generation == "zhipu_video_free"
