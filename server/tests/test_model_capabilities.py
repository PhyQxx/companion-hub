from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import ConfigStore, DatabaseConfigStore
from app.db import Base, create_database
from app.llm import EnvSecretProvider
from app.main import create_app
from app.model_capabilities import (
    CapabilityModelError,
    CapabilityModelService,
    JsonRequester,
)
from app.privacy import EgressBlocked
from app.schemas import PrivacyLevel


def capability_yaml() -> str:
    return """
schema_version: 1
models:
  cloud_text:
    kind: text
    provider: openai_compatible
    model: glm-4.7-flash
    base_url: https://open.bigmodel.cn/api/paas/v4
    secret_ref: env:ZAI_API_KEY
    runs_local: false
    max_privacy_level: L1
  local_text:
    kind: text
    provider: openai_compatible
    model: local-model
    base_url: http://127.0.0.1:1234/v1
    runs_local: true
    max_privacy_level: L2
  vision:
    kind: vision
    provider: zhipu_native
    model: glm-4.6v-flash
    base_url: https://open.bigmodel.cn/api/paas/v4
    secret_ref: env:ZAI_API_KEY
    runs_local: false
    max_privacy_level: L1
    max_tokens: 1024
  image:
    kind: image_generation
    provider: zhipu_native
    model: cogview-3-flash
    base_url: https://open.bigmodel.cn/api/paas/v4
    secret_ref: env:ZAI_API_KEY
    runs_local: false
    max_privacy_level: L1
  video:
    kind: video_generation
    provider: zhipu_native
    model: cogvideox-flash
    base_url: https://open.bigmodel.cn/api/paas/v4
    secret_ref: env:ZAI_API_KEY
    runs_local: false
    max_privacy_level: L1
routes:
  dialogue: {primary: cloud_text}
  utility: {primary: cloud_text}
  private: {primary: local_text}
capability_models:
  vision: vision
  image_generation: image
  video_generation: video
"""


async def _service(
    tmp_path: Path,
    requester: JsonRequester,
) -> CapabilityModelService:
    path = tmp_path / "hub.yaml"
    path.write_text(capability_yaml(), encoding="utf-8")
    store = ConfigStore(path)
    await store.load()
    return CapabilityModelService(
        store,
        secrets=EnvSecretProvider({"ZAI_API_KEY": "test-key"}),
        request_json=requester,
    )


async def test_vision_uses_glm46v_and_multimodal_chat_shape(tmp_path: Path) -> None:
    calls: list[tuple[str, str, dict[str, str], dict[str, object] | None]] = []

    def requester(
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object] | None,
        timeout: float,
    ) -> object:
        del timeout
        calls.append((method, url, headers, payload))
        return {
            "id": "vision-request",
            "choices": [{"message": {"content": "图片里有一只猫"}}],
        }

    service = await _service(tmp_path, requester)
    result = await service.analyze_vision(
        prompt="图里有什么?",
        image_urls=("https://example.com/cat.png",),
        privacy_level=PrivacyLevel.L1,
    )

    assert result.model == "glm-4.6v-flash"
    assert result.text == "图片里有一只猫"
    method, url, headers, payload = calls[0]
    assert method == "POST"
    assert url.endswith("/chat/completions")
    assert headers["Authorization"] == "Bearer test-key"
    assert payload is not None
    assert payload["model"] == "glm-4.6v-flash"
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["max_tokens"] == 1024


async def test_local_openai_vision_accepts_l2_data_url_without_secret(
    tmp_path: Path,
) -> None:
    calls: list[tuple[dict[str, str], dict[str, object] | None]] = []

    def requester(
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object] | None,
        timeout: float,
    ) -> object:
        del method, url, timeout
        calls.append((headers, payload))
        return {"choices": [{"message": {"content": "本地屏幕分析"}}]}

    local_vision = """  vision:
    kind: vision
    provider: openai_compatible
    model: local-vision
    base_url: http://127.0.0.1:1234/v1
    runs_local: true
    max_privacy_level: L2
"""
    cloud_vision = """  vision:
    kind: vision
    provider: zhipu_native
    model: glm-4.6v-flash
    base_url: https://open.bigmodel.cn/api/paas/v4
    secret_ref: env:ZAI_API_KEY
    runs_local: false
    max_privacy_level: L1
"""
    path = tmp_path / "local-vision.yaml"
    path.write_text(capability_yaml().replace(cloud_vision, local_vision), encoding="utf-8")
    store = ConfigStore(path)
    await store.load()
    service = CapabilityModelService(store, request_json=requester)

    result = await service.analyze_vision(
        prompt="描述屏幕",
        image_urls=("data:image/png;base64,iVBORw0KGgo=",),
        privacy_level=PrivacyLevel.L2,
        thinking=False,
    )

    assert result.text == "本地屏幕分析"
    headers, payload = calls[0]
    assert headers == {}
    assert payload is not None and "thinking" not in payload


async def test_image_generation_uses_cogview3_flash(tmp_path: Path) -> None:
    payloads: list[dict[str, object]] = []

    def requester(
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object] | None,
        timeout: float,
    ) -> object:
        del method, headers, timeout
        assert url.endswith("/images/generations")
        assert payload is not None
        payloads.append(payload)
        return {"created": 123, "data": [{"url": "https://example.com/generated.png"}]}

    service = await _service(tmp_path, requester)
    result = await service.generate_image(
        prompt="月光下的城市",
        size="1024x1024",
        user_id="user-123456",
    )

    assert result.model == "cogview-3-flash"
    assert result.urls == ("https://example.com/generated.png",)
    assert payloads[0]["model"] == "cogview-3-flash"
    assert payloads[0]["quality"] == "standard"


async def test_video_generation_and_async_result_use_cogvideox_flash(tmp_path: Path) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def requester(
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object] | None,
        timeout: float,
    ) -> object:
        del headers, timeout
        calls.append((method, url, payload))
        if method == "POST":
            return {
                "model": "cogvideox-flash",
                "id": "task-1",
                "request_id": "request-1",
                "task_status": "PROCESSING",
            }
        return {
            "model": "cogvideox-flash",
            "task_status": "SUCCESS",
            "video_result": [
                {
                    "url": "https://example.com/video.mp4",
                    "cover_image_url": "https://example.com/cover.jpg",
                }
            ],
        }

    service = await _service(tmp_path, requester)
    task = await service.generate_video(
        prompt="星空缓慢旋转",
        quality="quality",
        with_audio=True,
        user_id="user-123456",
    )
    result = await service.async_result(task.task_id)

    assert task.model == "cogvideox-flash"
    assert task.task_id == "task-1"
    assert calls[0][1].endswith("/videos/generations")
    assert calls[0][2] is not None and calls[0][2]["model"] == "cogvideox-flash"
    assert calls[1][0] == "GET"
    assert calls[1][1].endswith("/async-result/task-1")
    assert result.video_urls == ("https://example.com/video.mp4",)


async def test_cloud_capability_models_reject_l2_before_http(tmp_path: Path) -> None:
    called = False

    def requester(
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object] | None,
        timeout: float,
    ) -> object:
        del method, url, headers, payload, timeout
        nonlocal called
        called = True
        return {}

    service = await _service(tmp_path, requester)
    with pytest.raises(EgressBlocked):
        await service.generate_image(
            prompt="private",
            privacy_level=PrivacyLevel.L2,
        )
    assert called is False


async def test_capability_model_retries_transient_provider_overload(tmp_path: Path) -> None:
    attempts = 0

    def requester(
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object] | None,
        timeout: float,
    ) -> object:
        del method, url, headers, payload, timeout
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise CapabilityModelError(
                "provider_http_error",
                detail="HTTP 429: provider busy",
            )
        return {"choices": [{"message": {"content": "重试成功"}}]}

    path = tmp_path / "retry.yaml"
    path.write_text(
        capability_yaml().replace(
            "    max_tokens: 1024\n  image:",
            "    max_tokens: 1024\n    max_retries: 2\n  image:",
            1,
        ),
        encoding="utf-8",
    )
    store = ConfigStore(path)
    await store.load()
    service = CapabilityModelService(
        store,
        secrets=EnvSecretProvider({"ZAI_API_KEY": "test-key"}),
        request_json=requester,
    )

    result = await service.analyze_vision(
        prompt="描述图片",
        image_urls=("https://example.com/test.png",),
        privacy_level=PrivacyLevel.L1,
    )

    assert result.text == "重试成功"
    assert attempts == 3


async def test_configured_app_openapi_includes_capability_routes(tmp_path: Path) -> None:
    path = tmp_path / "hub.yaml"
    path.write_text(capability_yaml(), encoding="utf-8")
    database = create_database("sqlite+aiosqlite:///:memory:")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    store = DatabaseConfigStore(database, path)
    app = create_app(
        database,
        config_store=store,
        watch_config=False,
        admin_token="test-admin-token",
    )
    try:
        async with app.router.lifespan_context(app), AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/openapi.json")
        assert response.status_code == 200, response.text
        paths = response.json()["paths"]
        assert "/api/v1/model-capabilities/vision/analyze" in paths
        assert "/api/v1/model-capabilities/images/generate" in paths
        assert "/api/v1/model-capabilities/videos/generate" in paths
        assert "/api/v1/model-capabilities/tasks/{task_id}" in paths
    finally:
        await database.close()
