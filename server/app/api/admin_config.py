# ruff: noqa: RUF001, RUF003
from __future__ import annotations

import asyncio
import hmac
import importlib.metadata
import importlib.util
import json
import re
from datetime import datetime
from time import perf_counter
from typing import Annotated
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field

from app.config import DatabaseConfigStore, DatabaseConfigVersion, HubConfig
from app.config.models import VoiceAsrConfig
from app.config.store import ConfigSnapshot, hash_config
from app.llm import EnvSecretProvider, LiteLLMProvider, ModelEndpoint, ModelKind
from app.observability import apply_observability
from app.schemas.common import StrictModel

_BEARER = HTTPBearer(auto_error=False)
AdminCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)]


class AdminTokenGuard:
    def __init__(self, token: str | None) -> None:
        self._token = token

    async def __call__(self, credentials: AdminCredentials) -> None:
        if not self._token:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="admin API is disabled until ARIA_ADMIN_TOKEN is configured",
            )
        if credentials is None or not hmac.compare_digest(
            credentials.credentials, self._token
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid admin credential")


class ConfigVersionView(StrictModel):
    version: int
    status: str
    content_hash: str
    created_by: str
    created_at: datetime
    published_at: datetime | None
    rollback_from_version: int | None
    config: HubConfig | None = None


class CurrentConfigView(StrictModel):
    version: int
    content_hash: str
    published_at: datetime
    rollback_from_version: int | None
    config: HubConfig


class ValidationResult(StrictModel):
    valid: bool
    content_hash: str


class ModelConnectionTestRequest(StrictModel):
    endpoint: ModelEndpoint


class ModelProbeItem(StrictModel):
    key: str
    display_name: str
    type: str
    loaded_instances: int


class ModelConnectionTestResult(StrictModel):
    ok: bool
    probe_kind: str
    target_url: str
    model: str
    latency_ms: float
    message: str
    error_type: str | None = None
    model_available: bool | None = None
    model_loaded: bool | None = None
    models: list[ModelProbeItem] = Field(default_factory=list)


class VoiceAsrEnvironmentCheckRequest(StrictModel):
    asr: VoiceAsrConfig


class VoiceAsrEnvironmentCheckResult(StrictModel):
    ok: bool
    provider: str
    model: str
    runs_local: bool
    dependency_available: bool | None = None
    package_version: str | None = None
    model_load_checked: bool = False
    message: str


_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|token|secret)\s*[:=]\s*)"
        r"['\"]?[^'\"\s,;}]+"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


def _safe_probe_detail(error: BaseException) -> str:
    detail = str(error).strip() or type(error).__name__
    for pattern in _SECRET_PATTERNS:
        detail = pattern.sub(
            lambda match: f"{match.group(1) if match.lastindex else ''}<redacted>",
            detail,
        )
    return detail[:800]


def _origin_url(value: str) -> str:
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    return urlunsplit((parsed.scheme, netloc, "", "", "")).rstrip("/")


def _lm_studio_models_url(base_url: str) -> str:
    return f"{_origin_url(base_url)}/api/v1/models"


def _fetch_json(
    url: str,
    *,
    headers: dict[str, str],
    timeout_seconds: float,
) -> object:
    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read(2_000_000)
    return json.loads(raw.decode("utf-8"))


def _model_probe_items(payload: object) -> list[ModelProbeItem]:
    if not isinstance(payload, dict):
        raise ValueError("model list response must be a JSON object")
    rows = payload.get("models")
    if not isinstance(rows, list):
        raise ValueError("model list response is missing models[]")
    results: list[ModelProbeItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row.get("key")
        if not isinstance(key, str) or not key:
            continue
        display_name = row.get("display_name")
        model_type = row.get("type")
        loaded_instances = row.get("loaded_instances")
        results.append(
            ModelProbeItem(
                key=key,
                display_name=display_name if isinstance(display_name, str) else key,
                type=model_type if isinstance(model_type, str) else "unknown",
                loaded_instances=len(loaded_instances) if isinstance(loaded_instances, list) else 0,
            )
        )
    return results


def _model_match(items: list[ModelProbeItem], configured_model: str) -> ModelProbeItem | None:
    expected = configured_model.strip().casefold()
    if not expected:
        return None
    for item in items:
        if expected in {item.key.casefold(), item.display_name.casefold()}:
            return item
    return None


def _authorization_headers(endpoint: ModelEndpoint) -> dict[str, str]:
    value = endpoint.secret_value
    if value is None and endpoint.secret_ref is not None:
        value = EnvSecretProvider().resolve(endpoint.secret_ref)
    return {"Authorization": f"Bearer {value}"} if value else {}


def _version_view(
    version: DatabaseConfigVersion,
    *,
    include_config: bool = False,
) -> ConfigVersionView:
    return ConfigVersionView(
        version=version.version,
        status=version.status,
        content_hash=version.content_hash,
        created_by=version.created_by,
        created_at=version.created_at,
        published_at=version.published_at,
        rollback_from_version=version.rollback_from_version,
        config=version.config if include_config else None,
    )


def _current_view(snapshot: ConfigSnapshot) -> CurrentConfigView:
    return CurrentConfigView(
        version=snapshot.version,
        content_hash=snapshot.content_hash,
        published_at=snapshot.published_at,
        rollback_from_version=snapshot.rollback_from,
        config=snapshot.config,
    )


def create_admin_config_router(
    store: DatabaseConfigStore,
    *,
    admin_token: str | None,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/config",
        tags=["admin-config"],
        dependencies=[Depends(AdminTokenGuard(admin_token))],
    )

    @router.get("/current", response_model=CurrentConfigView)
    async def current() -> CurrentConfigView:
        return _current_view(store.current)

    @router.put("/current", response_model=CurrentConfigView)
    async def update_current(config: HubConfig) -> CurrentConfigView:
        """Validate, persist and immediately activate the supplied configuration.

        The store keeps immutable revisions internally for auditability, but the
        admin UI treats model/routing configuration as a single live document.
        """
        try:
            draft = await store.create_draft(config, actor="admin")
            snapshot = await store.publish(draft.version, actor="admin")
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        apply_observability(snapshot.config)
        return _current_view(snapshot)

    @router.get("/versions", response_model=list[ConfigVersionView])
    async def versions() -> list[ConfigVersionView]:
        return [_version_view(version) for version in await store.list_versions()]

    @router.get("/versions/{version}", response_model=ConfigVersionView)
    async def version_detail(version: int) -> ConfigVersionView:
        try:
            result = await store.get_version(version)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        return _version_view(result, include_config=True)

    @router.post("/validate", response_model=ValidationResult)
    async def validate(config: HubConfig) -> ValidationResult:
        await store.validate(config)
        return ValidationResult(valid=True, content_hash=hash_config(config))

    @router.post("/models/test", response_model=ModelConnectionTestResult)
    async def test_model_connection(
        body: ModelConnectionTestRequest,
    ) -> ModelConnectionTestResult:
        endpoint = body.endpoint
        started = perf_counter()

        if endpoint.runs_local:
            target_url = _lm_studio_models_url(str(endpoint.base_url))
            try:
                payload = await asyncio.to_thread(
                    _fetch_json,
                    target_url,
                    headers=_authorization_headers(endpoint),
                    timeout_seconds=max(endpoint.timeout_ms / 1_000, 0.1),
                )
                items = _model_probe_items(payload)
                matched = _model_match(items, endpoint.model)
                available = matched is not None
                loaded = matched.loaded_instances > 0 if matched is not None else False
                if loaded:
                    message = "连接成功，配置模型已加载"
                elif available:
                    message = "连接成功，配置模型已存在但尚未加载"
                else:
                    message = "连接成功，但未在本地模型列表中找到配置模型"
                return ModelConnectionTestResult(
                    ok=True,
                    probe_kind="lm_studio_native_v1",
                    target_url=target_url,
                    model=endpoint.model,
                    latency_ms=(perf_counter() - started) * 1_000,
                    message=message,
                    model_available=available,
                    model_loaded=loaded,
                    models=items,
                )
            except HTTPError as error:
                if error.code != 404:
                    return ModelConnectionTestResult(
                        ok=False,
                        probe_kind="lm_studio_native_v1",
                        target_url=target_url,
                        model=endpoint.model,
                        latency_ms=(perf_counter() - started) * 1_000,
                        message=f"本地模型服务返回 HTTP {error.code}",
                        error_type="HTTPError",
                    )
                # 非 LM Studio 本地服务可能只有 OpenAI-compatible /v1 接口，
                # 404 时继续用现有 Provider probe 验证真实推理链路。
            except (URLError, TimeoutError, OSError, ValueError) as error:
                return ModelConnectionTestResult(
                    ok=False,
                    probe_kind="lm_studio_native_v1",
                    target_url=target_url,
                    model=endpoint.model,
                    latency_ms=(perf_counter() - started) * 1_000,
                    message=_safe_probe_detail(error),
                    error_type=type(error).__name__,
                )
            except Exception as error:
                return ModelConnectionTestResult(
                    ok=False,
                    probe_kind="lm_studio_native_v1",
                    target_url=target_url,
                    model=endpoint.model,
                    latency_ms=(perf_counter() - started) * 1_000,
                    message=_safe_probe_detail(error),
                    error_type=type(error).__name__,
                )

        if endpoint.provider == "zhipu_native":
            # Media/vision APIs use capability-specific request bodies. A connection
            # button should not generate an image/video just to prove credentials.
            # Probe the same Zhipu account/base URL with the selected free text model.
            try:
                probe_endpoint = ModelEndpoint(
                    enabled=True,
                    kind=ModelKind.TEXT,
                    provider="openai_compatible",
                    model="glm-4.7-flash",
                    supports_json_mode=False,
                    thinking_mode="disabled",
                    base_url=endpoint.base_url,
                    secret_ref=endpoint.secret_ref,
                    secret_value=endpoint.secret_value,
                    runs_local=False,
                    max_privacy_level=endpoint.max_privacy_level,
                    timeout_ms=endpoint.timeout_ms,
                    max_retries=0,
                    max_context_tokens=204_800,
                    input_cost_per_million=0,
                    output_cost_per_million=0,
                )
                provider = LiteLLMProvider(
                    "admin_zhipu_account_probe",
                    probe_endpoint,
                    EnvSecretProvider(),
                )
                await provider.probe()
            except Exception as error:
                return ModelConnectionTestResult(
                    ok=False,
                    probe_kind="zhipu_account_probe",
                    target_url=str(endpoint.base_url),
                    model=endpoint.model,
                    latency_ms=(perf_counter() - started) * 1_000,
                    message=_safe_probe_detail(error),
                    error_type=type(error).__name__,
                )
            return ModelConnectionTestResult(
                ok=True,
                probe_kind="zhipu_account_probe",
                target_url=str(endpoint.base_url),
                model=endpoint.model,
                latency_ms=(perf_counter() - started) * 1_000,
                message="智谱账号连接正常；该能力模型将在实际调用时使用对应接口",
            )

        try:
            provider = LiteLLMProvider("admin_connection_test", endpoint, EnvSecretProvider())
            await provider.probe()
        except Exception as error:
            return ModelConnectionTestResult(
                ok=False,
                probe_kind="provider_probe",
                target_url=str(endpoint.base_url),
                model=endpoint.model,
                latency_ms=(perf_counter() - started) * 1_000,
                message=_safe_probe_detail(error),
                error_type=type(error).__name__,
            )
        return ModelConnectionTestResult(
            ok=True,
            probe_kind="provider_probe",
            target_url=str(endpoint.base_url),
            model=endpoint.model,
            latency_ms=(perf_counter() - started) * 1_000,
            message="连接成功，模型探针响应正常",
        )

    @router.post("/voice/asr/check", response_model=VoiceAsrEnvironmentCheckResult)
    async def check_voice_asr_environment(
        body: VoiceAsrEnvironmentCheckRequest,
    ) -> VoiceAsrEnvironmentCheckResult:
        """检查 ASR 本地运行前置条件。不下载/加载模型。不发送音频。"""
        asr = body.asr
        if asr.provider != "faster_whisper":
            return VoiceAsrEnvironmentCheckResult(
                ok=True,
                provider=asr.provider,
                model=asr.model,
                runs_local=asr.runs_local,
                message="MiMo 为云端 ASR，本地依赖自检不适用；请使用语音真连验证。",
            )

        available = importlib.util.find_spec("faster_whisper") is not None
        package_version: str | None = None
        if available:
            try:
                package_version = importlib.metadata.version("faster-whisper")
            except importlib.metadata.PackageNotFoundError:
                package_version = None
        return VoiceAsrEnvironmentCheckResult(
            ok=available,
            provider=asr.provider,
            model=asr.model,
            runs_local=asr.runs_local,
            dependency_available=available,
            package_version=package_version,
            model_load_checked=False,
            message=(
                "faster-whisper 已安装，可以继续做模型加载/真实转写验收"
                if available
                else "当前 Hub Python 环境未安装 faster-whisper"
            ),
        )

    @router.post(
        "/versions",
        response_model=ConfigVersionView,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_draft(config: HubConfig) -> ConfigVersionView:
        result = await store.create_draft(config, actor="admin")
        return _version_view(result, include_config=True)

    @router.post("/versions/{version}/publish", response_model=CurrentConfigView)
    async def publish(version: int) -> CurrentConfigView:
        try:
            snapshot = await store.publish(version, actor="admin")
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        apply_observability(snapshot.config)
        return _current_view(snapshot)

    @router.post("/versions/{version}/rollback", response_model=CurrentConfigView)
    async def rollback(version: int) -> CurrentConfigView:
        try:
            snapshot = await store.rollback(version, actor="admin")
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        apply_observability(snapshot.config)
        return _current_view(snapshot)

    return router
