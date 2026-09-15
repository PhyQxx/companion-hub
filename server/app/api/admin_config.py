from __future__ import annotations

import asyncio
import hmac
import importlib.metadata
import importlib.util
import json
import logging
import re
from collections.abc import Awaitable, Callable
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
from app.config.models import HomeAssistantConfig, VoiceAsrConfig
from app.config.store import ConfigSnapshot, hash_config
from app.home_assistant import HomeAssistantClient, HomeAssistantError
from app.llm import EnvSecretProvider, LiteLLMProvider, ModelEndpoint, ModelKind
from app.observability import apply_observability
from app.schemas.common import StrictModel
from app.tools import AmapProvider, AmapProviderError, ToolLedger

_BEARER = HTTPBearer(auto_error=False)
AdminCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)]

logger = logging.getLogger(__name__)
# 掩码必须能通过所有被脱敏字段的长度校验（vapid 私钥 min_length=32）
_SECRET_MASK = "__ARIA_SECRET_CONFIGURED__DO_NOT_EDIT__"

_runtime_admin_token: str | None = None


def set_runtime_admin_token(token: str | None) -> None:
    """设置运行时的 admin token，覆盖构造时传入的固定值。

    用于后台管理界面中修改 admin token 后即时生效，无需重启服务。
    """
    global _runtime_admin_token
    _runtime_admin_token = token


class AdminTokenGuard:
    def __init__(self, token: str | None) -> None:
        self._token = token

    async def __call__(self, credentials: AdminCredentials) -> None:
        token = _runtime_admin_token if _runtime_admin_token is not None else self._token
        if not token:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="admin API is disabled until ARIA_ADMIN_TOKEN is configured",
            )
        if credentials is None or not hmac.compare_digest(credentials.credentials, token):
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


class AmapConnectionTestStep(StrictModel):
    name: str
    ok: bool
    latency_ms: float
    message: str
    error_type: str | None = None


class AmapConnectionTestRequest(StrictModel):
    base_url: str = "https://restapi.amap.com"
    secret_ref: str | None = None
    secret_value: str | None = None
    timeout_ms: int = 3_500
    max_retries: int = 1
    max_concurrency: int = 2
    requests_per_minute: int = 30


class AmapConnectionTestResult(StrictModel):
    ok: bool
    steps: list[AmapConnectionTestStep]
    latency_ms: float
    message: str


class AmapMetricsResult(StrictModel):
    total_calls: int
    success_rate: float | None
    p50_latency_ms: float | None
    p90_latency_ms: float | None
    cache_hit_rate: float | None
    failures: dict[str, int]


class AmapLedgerEntryView(StrictModel):
    tool_name: str
    ok: bool
    provider: str | None
    latency_ms: float
    timestamp: datetime
    reason_code: str | None = None
    cache_hit: bool = False
    location_source: str | None = None
    result_count: int | None = None


class HomeAssistantConnectionTestRequest(StrictModel):
    config: HomeAssistantConfig


class HomeAssistantEntityView(StrictModel):
    entity_id: str
    friendly_name: str
    domain: str
    state: str
    device_class: str | None = None
    unit_of_measurement: str | None = None


class HomeAssistantConnectionTestResult(StrictModel):
    ok: bool
    latency_ms: float
    message: str
    error_type: str | None = None
    entities: list[HomeAssistantEntityView] = Field(default_factory=list)


class HomeAssistantEntityDetailView(StrictModel):
    entity_id: str
    friendly_name: str
    domain: str
    state: str
    device_class: str | None = None
    unit_of_measurement: str | None = None
    area: str | None = None
    device_id: str | None = None
    device_name: str | None = None
    manufacturer: str | None = None
    model: str | None = None


class HomeAssistantEntitiesResult(StrictModel):
    ok: bool
    latency_ms: float
    message: str
    error_type: str | None = None
    entities: list[HomeAssistantEntityDetailView] = Field(default_factory=list)


class HomeAssistantProactiveTestResult(StrictModel):
    ok: bool
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
        config=_redact_config(version.config) if include_config else None,
    )


def _current_view(snapshot: ConfigSnapshot) -> CurrentConfigView:
    return CurrentConfigView(
        version=snapshot.version,
        content_hash=snapshot.content_hash,
        published_at=snapshot.published_at,
        rollback_from_version=snapshot.rollback_from,
        config=_redact_config(snapshot.config),
    )


def _redact_config(config: HubConfig) -> HubConfig:
    data = config.model_dump(mode="python")
    xiaoai = data["integrations"]["xiaoai"]
    if xiaoai.get("xiaomi_password_secret_value"):
        xiaoai["xiaomi_password_secret_value"] = _SECRET_MASK
    if xiaoai.get("xiaomi_pass_token_secret_value"):
        xiaoai["xiaomi_pass_token_secret_value"] = _SECRET_MASK
    if xiaoai.get("gateway_token_secret_value"):
        xiaoai["gateway_token_secret_value"] = _SECRET_MASK
    push = data["integrations"]["push"]
    if push.get("vapid_private_key_secret_value"):
        push["vapid_private_key_secret_value"] = _SECRET_MASK
    for server in data["mcp"]["servers"]:
        if server.get("secret_value"):
            server["secret_value"] = _SECRET_MASK
    senseaudio = data["voice"]["senseaudio"]
    if senseaudio.get("secret_value"):
        senseaudio["secret_value"] = _SECRET_MASK
    return HubConfig.model_validate(data)


def _restore_secret_masks(config: HubConfig, current: HubConfig) -> HubConfig:
    data = config.model_dump(mode="python")
    incoming = data["integrations"]["xiaoai"]
    existing = current.integrations.xiaoai
    if incoming.get("xiaomi_password_secret_value") == _SECRET_MASK:
        incoming["xiaomi_password_secret_value"] = existing.xiaomi_password_secret_value
    if incoming.get("xiaomi_pass_token_secret_value") == _SECRET_MASK:
        incoming["xiaomi_pass_token_secret_value"] = existing.xiaomi_pass_token_secret_value
    if incoming.get("gateway_token_secret_value") == _SECRET_MASK:
        incoming["gateway_token_secret_value"] = existing.gateway_token_secret_value
    incoming_push = data["integrations"]["push"]
    existing_push = current.integrations.push
    if incoming_push.get("vapid_private_key_secret_value") == _SECRET_MASK:
        incoming_push["vapid_private_key_secret_value"] = (
            existing_push.vapid_private_key_secret_value
        )
    existing_mcp = {server.server_id: server for server in current.mcp.servers}
    for server in data["mcp"]["servers"]:
        if server.get("secret_value") == _SECRET_MASK:
            previous = existing_mcp.get(server.get("server_id"))
            server["secret_value"] = previous.secret_value if previous else None
    if data["voice"]["senseaudio"].get("secret_value") == _SECRET_MASK:
        data["voice"]["senseaudio"]["secret_value"] = current.voice.senseaudio.secret_value
    return HubConfig.model_validate(data)


def create_admin_config_router(
    store: DatabaseConfigStore,
    *,
    admin_token: str | None,
    on_publish: Callable[[], Awaitable[None]] | None = None,
    on_proactive_test: Callable[[], Awaitable[bool]] | None = None,
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
            config = _restore_secret_masks(config, store.current.config)
            draft = await store.create_draft(config, actor="admin")
            snapshot = await store.publish(draft.version, actor="admin")
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
        apply_observability(snapshot.config)
        if on_publish is not None:
            await on_publish()
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

    @router.post("/tools/amap/test", response_model=AmapConnectionTestResult)
    async def test_amap_connection(
        body: AmapConnectionTestRequest,
    ) -> AmapConnectionTestResult:
        """验证高德 Key 是否有效、配额是否正常、核心接口能否返回合法数据。"""
        started = perf_counter()
        steps: list[AmapConnectionTestStep] = []

        # 1. 解析 Key
        key_started = perf_counter()
        api_key: str | None = None
        try:
            if body.secret_value is not None:
                api_key = body.secret_value
            elif body.secret_ref is not None:
                api_key = EnvSecretProvider().resolve(body.secret_ref)
            if not api_key:
                steps.append(
                    AmapConnectionTestStep(
                        name="key_resolve",
                        ok=False,
                        latency_ms=(perf_counter() - key_started) * 1_000,
                        message="未配置高德 Key（secret_value 或 secret_ref 为空）",
                    )
                )
                return AmapConnectionTestResult(
                    ok=False,
                    steps=steps,
                    latency_ms=(perf_counter() - started) * 1_000,
                    message="Key 未配置",
                )
            steps.append(
                AmapConnectionTestStep(
                    name="key_resolve",
                    ok=True,
                    latency_ms=(perf_counter() - key_started) * 1_000,
                    message="Key 已解析",
                )
            )
        except Exception as error:
            steps.append(
                AmapConnectionTestStep(
                    name="key_resolve",
                    ok=False,
                    latency_ms=(perf_counter() - key_started) * 1_000,
                    message=str(error),
                    error_type=type(error).__name__,
                )
            )
            return AmapConnectionTestResult(
                ok=False,
                steps=steps,
                latency_ms=(perf_counter() - started) * 1_000,
                message=f"Key 解析失败: {error}",
            )

        provider = AmapProvider(
            api_key,
            base_url=str(body.base_url).rstrip("/"),
            timeout_ms=body.timeout_ms,
            max_retries=0,
            max_concurrency=body.max_concurrency,
            requests_per_minute=body.requests_per_minute,
        )
        try:
            # 2. 地理编码测试
            geo_started = perf_counter()
            try:
                geocode_result = await provider.geocode("济南市")
                steps.append(
                    AmapConnectionTestStep(
                        name="geocode",
                        ok=True,
                        latency_ms=(perf_counter() - geo_started) * 1_000,
                        message=f"地理编码正常，返回 adcode={geocode_result.get('adcode')}",
                    )
                )
            except AmapProviderError as error:
                steps.append(
                    AmapConnectionTestStep(
                        name="geocode",
                        ok=False,
                        latency_ms=(perf_counter() - geo_started) * 1_000,
                        message=error.reason_code,
                        error_type="AmapProviderError",
                    )
                )
            except Exception as error:
                steps.append(
                    AmapConnectionTestStep(
                        name="geocode",
                        ok=False,
                        latency_ms=(perf_counter() - geo_started) * 1_000,
                        message=str(error),
                        error_type=type(error).__name__,
                    )
                )

            # 3. 天气接口测试（使用济南 adcode 370100）
            weather_started = perf_counter()
            try:
                weather_result = await provider.weather("370100", extensions="base")
                lives = weather_result.get("lives")
                if isinstance(lives, list) and lives:
                    steps.append(
                        AmapConnectionTestStep(
                            name="weather",
                            ok=True,
                            latency_ms=(perf_counter() - weather_started) * 1_000,
                            message=f"天气接口正常，返回城市={lives[0].get('city')}",
                        )
                    )
                else:
                    steps.append(
                        AmapConnectionTestStep(
                            name="weather",
                            ok=False,
                            latency_ms=(perf_counter() - weather_started) * 1_000,
                            message="天气接口返回数据异常",
                        )
                    )
            except AmapProviderError as error:
                steps.append(
                    AmapConnectionTestStep(
                        name="weather",
                        ok=False,
                        latency_ms=(perf_counter() - weather_started) * 1_000,
                        message=error.reason_code,
                        error_type="AmapProviderError",
                    )
                )
            except Exception as error:
                steps.append(
                    AmapConnectionTestStep(
                        name="weather",
                        ok=False,
                        latency_ms=(perf_counter() - weather_started) * 1_000,
                        message=str(error),
                        error_type=type(error).__name__,
                    )
                )
        finally:
            await provider.close()

        all_ok = all(step.ok for step in steps)
        failed = [step.name for step in steps if not step.ok]
        return AmapConnectionTestResult(
            ok=all_ok,
            steps=steps,
            latency_ms=(perf_counter() - started) * 1_000,
            message="全部通过" if all_ok else f"失败项: {', '.join(failed)}",
        )

    @router.post(
        "/integrations/home-assistant/test",
        response_model=HomeAssistantConnectionTestResult,
    )
    async def test_home_assistant_connection(
        body: HomeAssistantConnectionTestRequest,
    ) -> HomeAssistantConnectionTestResult:
        """Validate HA credentials and return a safe entity inventory for allowlisting."""
        config = body.config
        started = perf_counter()
        if config.base_url is None:
            return HomeAssistantConnectionTestResult(
                ok=False,
                latency_ms=0,
                message="请先填写 Home Assistant 地址",
                error_type="ha_config_invalid",
            )
        try:
            token = config.secret_value
            if token is None and config.secret_ref is not None:
                token = EnvSecretProvider().resolve(config.secret_ref)
            if not token:
                raise HomeAssistantError("ha_secret_unavailable")
            client = HomeAssistantClient(
                str(config.base_url).rstrip("/"),
                token,
                verify_tls=config.verify_tls,
                connect_timeout_ms=config.connect_timeout_ms,
                request_timeout_ms=config.request_timeout_ms,
            )
            try:
                states = await client.fetch_states()
            finally:
                await client.close()
        except Exception as error:
            reason = (
                error.reason_code if isinstance(error, HomeAssistantError) else type(error).__name__
            )
            return HomeAssistantConnectionTestResult(
                ok=False,
                latency_ms=(perf_counter() - started) * 1_000,
                message=f"连接失败：{reason}",
                error_type=reason,
            )
        entities = [
            HomeAssistantEntityView(
                entity_id=state.entity_id,
                friendly_name=str(state.attributes.get("friendly_name") or state.entity_id),
                domain=state.entity_id.split(".", 1)[0],
                state=state.state,
                device_class=(
                    str(state.attributes["device_class"])
                    if state.attributes.get("device_class") is not None
                    else None
                ),
                unit_of_measurement=(
                    str(state.attributes["unit_of_measurement"])
                    if state.attributes.get("unit_of_measurement") is not None
                    else None
                ),
            )
            for state in sorted(states, key=lambda item: item.entity_id)
        ]
        return HomeAssistantConnectionTestResult(
            ok=True,
            latency_ms=(perf_counter() - started) * 1_000,
            message=f"连接成功，发现 {len(entities)} 个实体",
            entities=entities,
        )

    @router.post(
        "/integrations/home-assistant/entities",
        response_model=HomeAssistantEntitiesResult,
    )
    async def list_home_assistant_entities() -> HomeAssistantEntitiesResult:
        """拉取当前配置下 Home Assistant 的所有实体，包含区域信息。"""
        config = store.current.config.integrations.home_assistant
        started = perf_counter()
        if not config.enabled or config.base_url is None:
            return HomeAssistantEntitiesResult(
                ok=False,
                latency_ms=0,
                message="Home Assistant 未启用或未配置地址",
                error_type="ha_config_invalid",
            )
        try:
            token = config.secret_value
            if token is None and config.secret_ref is not None:
                token = EnvSecretProvider().resolve(config.secret_ref)
            if not token:
                raise HomeAssistantError("ha_secret_unavailable")
            client = HomeAssistantClient(
                str(config.base_url).rstrip("/"),
                token,
                verify_tls=config.verify_tls,
                connect_timeout_ms=config.connect_timeout_ms,
                request_timeout_ms=config.request_timeout_ms,
            )
            try:
                states = await client.fetch_states()
                try:
                    areas = await client.fetch_entity_areas()
                except HomeAssistantError as area_error:
                    logger.warning(
                        "home assistant entity areas fetch failed: %s", area_error.reason_code
                    )
                    areas = {}
                try:
                    devices = await client.fetch_entity_devices()
                except HomeAssistantError as device_error:
                    logger.warning(
                        "home assistant entity devices fetch failed: %s",
                        device_error.reason_code,
                    )
                    devices = {}
            finally:
                await client.close()
        except Exception as error:
            reason = (
                error.reason_code if isinstance(error, HomeAssistantError) else type(error).__name__
            )
            return HomeAssistantEntitiesResult(
                ok=False,
                latency_ms=(perf_counter() - started) * 1_000,
                message=f"连接失败：{reason}",
                error_type=reason,
            )
        entities = []
        for state in sorted(states, key=lambda item: item.entity_id):
            device = devices.get(state.entity_id, {})
            entities.append(
                HomeAssistantEntityDetailView(
                    entity_id=state.entity_id,
                    friendly_name=str(state.attributes.get("friendly_name") or state.entity_id),
                    domain=state.entity_id.split(".", 1)[0],
                    state=state.state,
                    device_class=(
                        str(state.attributes["device_class"])
                        if state.attributes.get("device_class") is not None
                        else None
                    ),
                    unit_of_measurement=(
                        str(state.attributes["unit_of_measurement"])
                        if state.attributes.get("unit_of_measurement") is not None
                        else None
                    ),
                    area=areas.get(state.entity_id) or None,
                    device_id=device.get("device_id"),
                    device_name=device.get("name"),
                    manufacturer=device.get("manufacturer"),
                    model=device.get("model"),
                )
            )
        return HomeAssistantEntitiesResult(
            ok=True,
            latency_ms=(perf_counter() - started) * 1_000,
            message=f"连接成功，发现 {len(entities)} 个实体",
            entities=entities,
        )

    @router.post(
        "/integrations/home-assistant/proactive/test",
        response_model=HomeAssistantProactiveTestResult,
    )
    async def test_home_assistant_proactive() -> HomeAssistantProactiveTestResult:
        if on_proactive_test is None:
            return HomeAssistantProactiveTestResult(ok=False, message="主动感知服务尚未启动")
        delivered = await on_proactive_test()
        return HomeAssistantProactiveTestResult(
            ok=delivered,
            message=(
                "测试提醒已发送到最近使用的聊天" if delivered else "没有可接收测试提醒的活动聊天"
            ),
        )

    @router.get("/tools/amap/metrics", response_model=AmapMetricsResult)
    async def amap_metrics() -> AmapMetricsResult:
        """基于最近 200 次工具调用台账聚合延迟报告。"""
        raw = ToolLedger().metrics()
        return AmapMetricsResult(
            total_calls=raw["total_calls"],
            success_rate=raw["success_rate"],
            p50_latency_ms=raw["p50_latency_ms"],
            p90_latency_ms=raw["p90_latency_ms"],
            cache_hit_rate=raw["cache_hit_rate"],
            failures=raw["failures"],
        )

    @router.get("/tools/amap/ledger", response_model=list[AmapLedgerEntryView])
    async def amap_ledger(limit: int = 50) -> list[AmapLedgerEntryView]:
        """返回最近工具调用台账(脱敏)。"""
        entries = ToolLedger().snapshot()
        return [
            AmapLedgerEntryView(
                tool_name=e.tool_name,
                ok=e.ok,
                provider=e.provider,
                latency_ms=e.latency_ms,
                timestamp=e.timestamp,
                reason_code=e.reason_code,
                cache_hit=e.cache_hit,
                location_source=e.location_source,
                result_count=e.result_count,
            )
            for e in entries[:limit]
        ]

    @router.get(
        "/integrations/home-assistant/ledger",
        response_model=list[AmapLedgerEntryView],
    )
    async def home_assistant_ledger(limit: int = 50) -> list[AmapLedgerEntryView]:
        """返回最近 Home Assistant 查询与控制调用的脱敏台账。"""
        entries = ToolLedger().snapshot(provider="home_assistant")
        return [
            AmapLedgerEntryView(
                tool_name=e.tool_name,
                ok=e.ok,
                provider=e.provider,
                latency_ms=e.latency_ms,
                timestamp=e.timestamp,
                reason_code=e.reason_code,
                cache_hit=e.cache_hit,
                location_source=e.location_source,
                result_count=e.result_count,
            )
            for e in entries[:limit]
        ]

    @router.post(
        "/versions",
        response_model=ConfigVersionView,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_draft(config: HubConfig) -> ConfigVersionView:
        config = _restore_secret_masks(config, store.current.config)
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
        if on_publish is not None:
            await on_publish()
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
        if on_publish is not None:
            await on_publish()
        return _current_view(snapshot)

    return router
