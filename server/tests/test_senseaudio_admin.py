"""admin 声音管理（SenseAudio 代理）接口测试。

Fake 客户端替换 admin_senseaudio.SenseAudioClient，覆盖：连接配置保存与掩码
往返、未配置/未启用拦截、音色目录、试听合成、识别历史与上游错误映射。
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import DatabaseConfigStore
from app.db import Base, Database, create_database
from app.integrations.senseaudio import (
    SenseAudioCloneFile,
    SenseAudioCloneResult,
    SenseAudioError,
    SenseAudioSynthesis,
    SenseAudioVoice,
)
from app.main import create_app

MASK = "__ARIA_SECRET_CONFIGURED__DO_NOT_EDIT__"


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
    path.write_text(
        """
schema_version: 1
models:
  cloud:
    provider: openai_compatible
    model: test-model
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
  dialogue: {primary: cloud}
  utility: {primary: cloud}
  private: {primary: local}
""",
        encoding="utf-8",
    )
    return path


class FakeSenseAudioClient:
    instances: ClassVar[list[FakeSenseAudioClient]] = []
    fail_with: ClassVar[str | None] = None

    def __init__(self, api_key: str, *, base_url: str, tts_model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.tts_model = tts_model
        FakeSenseAudioClient.instances.append(self)

    async def aclose(self) -> None:
        return None

    async def list_voices(self, voice_type: str = "all") -> list[SenseAudioVoice]:
        if FakeSenseAudioClient.fail_with:
            raise SenseAudioError(FakeSenseAudioClient.fail_with)
        return [
            SenseAudioVoice(
                category="system",
                voice_id="male_0004_a",
                voice_name="儒雅道长",
                description=("中年", "男声"),
                created_time="2026-01-01 00:00:00",
                free_tier=True,
            ),
            SenseAudioVoice(
                category="system",
                voice_id="male-qn-qingse",
                voice_name="青涩青年",
                description=("青年", "男声"),
                created_time="2026-01-01 00:00:00",
            ),
            SenseAudioVoice(
                category="voice_clone",
                voice_id="clone-abc",
                voice_name="我的克隆",
                description=(),
                created_time=None,
            ),
        ]

    async def synthesize(self, text: str, voice_id: str, **kwargs: object) -> SenseAudioSynthesis:
        if FakeSenseAudioClient.fail_with:
            raise SenseAudioError(FakeSenseAudioClient.fail_with)
        return SenseAudioSynthesis(
            audio=f"fake-audio:{voice_id}:{text}".encode(),
            audio_format="mp3",
            sample_rate=32000,
            usage_characters=len(text),
            audio_length=1.5,
            audio_size=64,
        )

    async def asr_records(
        self, *, page: int, page_size: int, session_id: str | None
    ) -> dict[str, object]:
        if FakeSenseAudioClient.fail_with:
            raise SenseAudioError(FakeSenseAudioClient.fail_with)
        return {
            "total": 1,
            "records": [
                {
                    "session_id": "sess-1",
                    "session_start": 1_767_000_000,
                    "session_end": 1_767_000_060,
                    "text": "今天天气怎么样",
                    "points": 3,
                    "audio": "https://cdn.example/pcm/sess-1",
                }
            ],
        }

    async def upload_clone_file(
        self, data: bytes, *, filename: str
    ) -> SenseAudioCloneFile:
        if FakeSenseAudioClient.fail_with:
            raise SenseAudioError(FakeSenseAudioClient.fail_with)
        return SenseAudioCloneFile(
            file_id=f"file-{len(data)}",
            filename=filename,
            size_bytes=len(data),
            created_at=1_767_000_000,
        )

    async def clone_voice(
        self,
        *,
        file_id: str,
        label: str,
        description: str,
        text: str,
        model: str | None = None,
    ) -> SenseAudioCloneResult:
        if FakeSenseAudioClient.fail_with:
            raise SenseAudioError(FakeSenseAudioClient.fail_with)
        return SenseAudioCloneResult(
            label=label,
            name=label,
            description=description,
            created_at=1_767_000_000,
            demo=f"https://cdn.example/clone/{label}.mp3",
        )


@pytest.fixture(autouse=True)
def fake_client(monkeypatch: pytest.MonkeyPatch) -> type[FakeSenseAudioClient]:
    FakeSenseAudioClient.instances = []
    FakeSenseAudioClient.fail_with = None
    monkeypatch.setattr(
        "app.api.admin_senseaudio.SenseAudioClient", FakeSenseAudioClient
    )
    return FakeSenseAudioClient


@pytest.fixture
async def client(
    database: Database, bootstrap: Path
) -> AsyncIterator[AsyncClient]:
    store = DatabaseConfigStore(database, bootstrap)
    app = create_app(
        database,
        config_store=store,
        watch_config=False,
        admin_token="test-admin-token",
    )
    headers = {"Authorization": "Bearer test-admin-token"}
    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=headers
    ) as http:
        yield http


async def _enable(client: AsyncClient) -> None:
    response = await client.put(
        "/api/v1/admin/senseaudio/connection",
        json={
            "enabled": True,
            "base_url": "https://api.senseaudio.cn",
            "secret_value": "sk-sense-test",
            "secret_ref": None,
            "tts_model": "sensenova-tts-2.0",
        },
    )
    assert response.status_code == 200


async def test_connection_roundtrip_masks_secret(client: AsyncClient) -> None:
    await _enable(client)

    status = (await client.get("/api/v1/admin/senseaudio/status")).json()
    assert status["enabled"] is True
    assert status["key_configured"] is True
    assert status["key_source"] == "inline"

    # 配置中心出参脱敏：密钥不回传明文
    current = (await client.get("/api/v1/admin/config/current")).json()
    assert current["config"]["voice"]["senseaudio"]["secret_value"] == MASK

    # 掩码回传不覆盖已存密钥；不填 Key 直接启用被拒绝
    masked = await client.put(
        "/api/v1/admin/senseaudio/connection",
        json={
            "enabled": True,
            "base_url": "https://api.senseaudio.cn",
            "secret_value": MASK,
            "secret_ref": None,
            "tts_model": "sensenova-tts-2.0",
        },
    )
    assert masked.status_code == 200
    stored = (await client.get("/api/v1/admin/config/current")).json()
    assert stored["config"]["voice"]["senseaudio"]["secret_value"] == MASK


async def test_enable_requires_api_key(client: AsyncClient) -> None:
    response = await client.put(
        "/api/v1/admin/senseaudio/connection",
        json={
            "enabled": True,
            "base_url": "https://api.senseaudio.cn",
            "secret_value": None,
            "secret_ref": None,
            "tts_model": "sensenova-tts-2.0",
        },
    )
    assert response.status_code == 422


async def test_voices_blocked_until_enabled(client: AsyncClient) -> None:
    response = await client.get("/api/v1/admin/senseaudio/voices")
    assert response.status_code == 409


async def test_voices_preview_and_records(client: AsyncClient) -> None:
    await _enable(client)

    voices = (await client.get("/api/v1/admin/senseaudio/voices?voice_type=all")).json()
    assert [item["voice_id"] for item in voices["voices"]] == [
        "male_0004_a",
        "male-qn-qingse",
        "clone-abc",
    ]
    assert voices["voices"][0]["category"] == "system"
    assert voices["voices"][0]["free_tier"] is True
    assert voices["voices"][1]["free_tier"] is False
    assert FakeSenseAudioClient.instances[-1].api_key == "sk-sense-test"

    preview = (
        await client.post(
            "/api/v1/admin/senseaudio/preview",
            json={"voice_id": "male-qn-qingse", "text": "你好"},
        )
    ).json()
    assert base64.b64decode(preview["audio_base64"]) == "fake-audio:male-qn-qingse:你好".encode()
    assert preview["usage_characters"] == 2
    assert preview["audio_format"] == "mp3"

    records = (
        await client.get("/api/v1/admin/senseaudio/asr/records?page=1&page_size=10")
    ).json()
    assert records["total"] == 1
    assert records["records"][0]["session_id"] == "sess-1"
    assert records["records"][0]["text"] == "今天天气怎么样"


async def test_upstream_error_maps_to_bad_gateway(client: AsyncClient) -> None:
    await _enable(client)
    FakeSenseAudioClient.fail_with = "invalid api key"

    response = await client.get("/api/v1/admin/senseaudio/voices")
    assert response.status_code == 502
    assert "invalid api key" in response.json()["detail"]


async def test_clone_upload_and_clone_voice(client: AsyncClient) -> None:
    await _enable(client)
    audio = b"RIFFfake-wav-data"

    upload = await client.post(
        "/api/v1/admin/senseaudio/clone/upload",
        files={"file": ("reference.wav", audio, "audio/wav")},
    )
    assert upload.status_code == 200
    file_body = upload.json()
    assert file_body["file_id"] == f"file-{len(audio)}"
    assert file_body["filename"] == "reference.wav"

    clone = await client.post(
        "/api/v1/admin/senseaudio/clone",
        json={
            "file_id": file_body["file_id"],
            "label": "my_voice",
            "description": "温柔的青年女声",
            "text": "你好，听听像不像",
        },
    )
    assert clone.status_code == 200
    clone_body = clone.json()
    assert clone_body["label"] == "my_voice"
    assert clone_body["demo"] == "https://cdn.example/clone/my_voice.mp3"


async def test_clone_upload_rejects_bad_format(client: AsyncClient) -> None:
    await _enable(client)

    response = await client.post(
        "/api/v1/admin/senseaudio/clone/upload",
        files={"file": ("clip.flac", b"flac-data", "audio/flac")},
    )
    assert response.status_code == 422
    assert "MP3/AAC/WAV" in response.json()["detail"]


async def test_clone_rejects_unsafe_label(client: AsyncClient) -> None:
    await _enable(client)

    response = await client.post(
        "/api/v1/admin/senseaudio/clone",
        json={
            "file_id": "file-1",
            "label": "我的音色!",
            "description": "desc",
            "text": "你好",
        },
    )
    assert response.status_code == 422


async def test_voice_access_error_maps_to_tier_guidance(client: AsyncClient) -> None:
    await _enable(client)
    FakeSenseAudioClient.fail_with = "no access to the specified voice"

    response = await client.post(
        "/api/v1/admin/senseaudio/preview",
        json={"voice_id": "male-qn-qingse", "text": "你好"},
    )
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "Free" in detail
    assert "Free 可用" in detail
    assert "no access to the specified voice" in detail


async def test_free_tier_flag_derived_from_documented_ids() -> None:
    from app.integrations.senseaudio import FREE_TIER_VOICE_IDS

    assert {
        "child_0001_a",
        "child_0001_b",
        "male_0004_a",
        "male_0018_a",
    } <= FREE_TIER_VOICE_IDS
    assert "male-qn-qingse" not in FREE_TIER_VOICE_IDS
