"""admin 声音管理：SenseAudio 连接配置、音色目录、试听合成、音色克隆与识别历史。

密钥只保存在配置中心，由 Hub 代理上游调用；浏览器管理端不接触明文 Key。
连接配置走配置中心草稿—发布流程，保存即热生效（与语音管线配置一致）。
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from time import perf_counter
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, ValidationError

from app.api.admin_config import AdminTokenGuard
from app.config import DatabaseConfigStore
from app.config.models import SenseAudioConfig
from app.integrations.senseaudio import (
    DEFAULT_TTS_MODEL,
    NO_VOICE_ACCESS_MESSAGE,
    SenseAudioClient,
    SenseAudioError,
    VoiceType,
)
from app.llm.provider import EnvSecretProvider, SecretNotFound

logger = logging.getLogger(__name__)

SECRET_MASK = "__ARIA_SECRET_CONFIGURED__DO_NOT_EDIT__"
MAX_PREVIEW_CHARS = 500
MAX_PREVIEW_BYTES = 2 * 1024 * 1024
# 参考音频约束（docs 自定义音色指南）：3-30 秒、50MB、MP3/AAC/WAV。
MAX_CLONE_FILE_BYTES = 50 * 1024 * 1024
CLONE_FILE_EXTENSIONS = {".mp3", ".aac", ".wav"}

SenseAudioClientFactory = Callable[[str, SenseAudioConfig], SenseAudioClient]


class SenseAudioStatusView(BaseModel):
    enabled: bool
    key_configured: bool
    key_source: Literal["inline", "env", "none"]
    base_url: str
    tts_model: str
    latency_probe_ok: bool | None = None


class SenseAudioConnectionRequest(BaseModel):
    enabled: bool
    base_url: str = Field(min_length=1, max_length=500)
    secret_value: str | None = Field(default=None, max_length=1024)
    secret_ref: str | None = Field(default=None, max_length=200)
    tts_model: str = Field(default=DEFAULT_TTS_MODEL, min_length=1, max_length=100)


class SenseAudioVoiceView(BaseModel):
    category: str
    voice_id: str
    voice_name: str
    description: list[str]
    created_time: str | None
    free_tier: bool = False


class SenseAudioVoiceCatalog(BaseModel):
    voices: list[SenseAudioVoiceView]


class SenseAudioPreviewRequest(BaseModel):
    voice_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=MAX_PREVIEW_CHARS)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    vol: float = Field(default=1.0, ge=0.01, le=10.0)
    pitch: int = Field(default=0, ge=-12, le=12)
    audio_format: Literal["mp3", "wav", "flac"] = "mp3"
    sample_rate: int = 32000
    model: str | None = Field(default=None, min_length=1, max_length=100)


class SenseAudioPreviewResult(BaseModel):
    audio_base64: str
    audio_format: str
    sample_rate: int | None
    usage_characters: int | None
    audio_length: float | None
    audio_size: int | None


class SenseAudioAsrRecords(BaseModel):
    total: int
    page: int
    page_size: int
    records: list[dict[str, Any]]


class SenseAudioCloneFileView(BaseModel):
    file_id: str
    filename: str
    size_bytes: int
    created_at: int | None


class SenseAudioCloneRequest(BaseModel):
    file_id: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    description: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=500)
    model: str | None = Field(default=None, min_length=1, max_length=100)


class SenseAudioCloneResultView(BaseModel):
    label: str
    name: str
    description: str
    created_at: int | None
    demo: str | None


def resolve_api_key(config: SenseAudioConfig) -> str | None:
    value = config.secret_value
    if value is None and config.secret_ref is not None:
        try:
            value = EnvSecretProvider().resolve(config.secret_ref)
        except SecretNotFound:
            return None
    return value or None


def _build_default_client(api_key: str, config: SenseAudioConfig) -> SenseAudioClient:
    return SenseAudioClient(
        api_key,
        base_url=str(config.base_url),
        tts_model=config.tts_model,
    )


def create_admin_senseaudio_router(
    store: DatabaseConfigStore,
    *,
    admin_token: str | None,
    client_factory: SenseAudioClientFactory = _build_default_client,
) -> APIRouter:

    router = APIRouter(
        prefix="/api/v1/admin/senseaudio",
        tags=["admin-senseaudio"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    def _settings() -> SenseAudioConfig:
        return store.current.config.voice.senseaudio

    def _status_view() -> SenseAudioStatusView:
        config = _settings()
        key = resolve_api_key(config)
        return SenseAudioStatusView(
            enabled=config.enabled,
            key_configured=key is not None,
            key_source=(
                "none" if key is None else ("env" if config.secret_ref else "inline")
            ),
            base_url=str(config.base_url),
            tts_model=config.tts_model,
        )

    def _client() -> SenseAudioClient:
        config = _settings()
        if not config.enabled:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="SenseAudio 未启用，请先在声音管理中开启并保存连接",
            )
        key = resolve_api_key(config)
        if not key:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="SenseAudio API Key 未配置",
            )
        return client_factory(key, config)

    @router.get("/status", response_model=SenseAudioStatusView)
    async def status_view() -> SenseAudioStatusView:
        return _status_view()

    @router.put("/connection", response_model=SenseAudioStatusView)
    async def update_connection(body: SenseAudioConnectionRequest) -> SenseAudioStatusView:
        current = _settings()
        secret_value = body.secret_value
        if secret_value == SECRET_MASK:
            secret_value = current.secret_value
        try:
            candidate = SenseAudioConfig(
                base_url=body.base_url,
                secret_ref=body.secret_ref or None,
                secret_value=secret_value or None,
                tts_model=body.tts_model,
                enabled=body.enabled,
            )
        except ValidationError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"连接参数不合法：{error.error_count()} 处校验失败",
            ) from error
        if candidate.enabled and resolve_api_key(candidate) is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="启用前需要配置 API Key（secret_value 或 secret_ref）",
            )
        snapshot = store.current
        data = snapshot.config.model_dump(mode="python")
        data["voice"]["senseaudio"] = candidate.model_dump(mode="python")
        from app.config import HubConfig

        draft = await store.create_draft(HubConfig.model_validate(data), actor="admin")
        await store.publish(draft.version, actor="admin")
        return _status_view()

    @router.get("/voices", response_model=SenseAudioVoiceCatalog)
    async def voices(voice_type: VoiceType = "all") -> SenseAudioVoiceCatalog:
        client = _client()
        try:
            items = await client.list_voices(voice_type)
        except SenseAudioError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
        finally:
            await client.aclose()
        return SenseAudioVoiceCatalog(
            voices=[
                SenseAudioVoiceView(
                    category=item.category,
                    voice_id=item.voice_id,
                    voice_name=item.voice_name,
                    description=list(item.description),
                    created_time=item.created_time,
                    free_tier=item.free_tier,
                )
                for item in items
            ]
        )

    @router.post("/preview", response_model=SenseAudioPreviewResult)
    async def preview(body: SenseAudioPreviewRequest) -> SenseAudioPreviewResult:
        client = _client()
        started = perf_counter()
        try:
            result = await client.synthesize(
                body.text,
                body.voice_id,
                model=body.model,
                speed=body.speed,
                vol=body.vol,
                pitch=body.pitch,
                audio_format=body.audio_format,
                sample_rate=body.sample_rate,
            )
        except SenseAudioError as error:
            if NO_VOICE_ACCESS_MESSAGE in str(error):
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    detail=(
                        f"音色 {body.voice_id} 不在当前套餐的可用范围"
                        "（Free 套餐只能合成标注「Free 可用」的普通音色）。"
                        f"可在音色库按「Free 可用」筛选后重试。{error}"
                    ),
                ) from error
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
        finally:
            await client.aclose()
        if len(result.audio) > MAX_PREVIEW_BYTES:
            raise HTTPException(
                413,
                detail="合成音频超过预览大小上限",
            )
        logger.info(
            "senseaudio preview voice=%s chars=%s bytes=%d latency_ms=%.0f",
            body.voice_id,
            result.usage_characters,
            len(result.audio),
            (perf_counter() - started) * 1_000,
        )
        return SenseAudioPreviewResult(
            audio_base64=base64.b64encode(result.audio).decode("ascii"),
            audio_format=result.audio_format,
            sample_rate=result.sample_rate,
            usage_characters=result.usage_characters,
            audio_length=result.audio_length,
            audio_size=result.audio_size,
        )

    @router.get("/asr/records", response_model=SenseAudioAsrRecords)
    async def asr_records(
        page: int = 1,
        page_size: int = 20,
        session_id: str | None = None,
    ) -> SenseAudioAsrRecords:
        client = _client()
        try:
            payload = await client.asr_records(
                page=page, page_size=page_size, session_id=session_id
            )
        except SenseAudioError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
        finally:
            await client.aclose()
        return SenseAudioAsrRecords(
            total=payload["total"],
            page=max(page, 1),
            page_size=max(min(page_size, 100), 1),
            records=payload["records"],
        )

    @router.post("/clone/upload", response_model=SenseAudioCloneFileView)
    async def clone_upload(
        file: Annotated[UploadFile, File()],
    ) -> SenseAudioCloneFileView:
        """上传克隆参考音频（3-30 秒清晰人声；MP3/AAC/WAV；50MB 以内）。"""
        filename = file.filename or "reference.wav"
        suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if suffix not in CLONE_FILE_EXTENSIONS:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"参考音频仅支持 MP3/AAC/WAV，收到 {suffix or '未知格式'}",
            )
        data = await file.read(MAX_CLONE_FILE_BYTES + 1)
        if len(data) > MAX_CLONE_FILE_BYTES:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="参考音频超过 50MB 上限",
            )
        if not data:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="参考音频为空文件",
            )
        client = _client()
        try:
            result = await client.upload_clone_file(data, filename=filename)
        except SenseAudioError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
        finally:
            await client.aclose()
        logger.info(
            "senseaudio clone upload filename=%s bytes=%d file_id=%s",
            result.filename,
            result.size_bytes,
            result.file_id,
        )
        return SenseAudioCloneFileView(
            file_id=result.file_id,
            filename=result.filename,
            size_bytes=result.size_bytes,
            created_at=result.created_at,
        )

    @router.post("/clone", response_model=SenseAudioCloneResultView)
    async def clone_voice(body: SenseAudioCloneRequest) -> SenseAudioCloneResultView:
        """发起音色克隆；label 即生成后的 voice_id。

        注意：克隆/文生音色共享套餐生成额度（Free 每期 2 次），超出按次计费。
        """
        client = _client()
        started = perf_counter()
        try:
            result = await client.clone_voice(
                file_id=body.file_id,
                label=body.label,
                description=body.description,
                text=body.text,
                model=body.model,
            )
        except SenseAudioError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
        finally:
            await client.aclose()
        logger.info(
            "senseaudio clone label=%s latency_ms=%.0f demo=%s",
            result.label,
            (perf_counter() - started) * 1_000,
            bool(result.demo),
        )
        return SenseAudioCloneResultView(
            label=result.label,
            name=result.name,
            description=result.description,
            created_at=result.created_at,
            demo=result.demo,
        )

    return router
