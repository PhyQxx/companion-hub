from __future__ import annotations

import re
from ipaddress import ip_address
from typing import Annotated, Any, Literal

from pydantic import AnyHttpUrl, Field, model_validator

from app.llm.contracts import LLMRoute, ModelEndpoint, ModelKind, RoutePolicy
from app.schemas.common import StrictModel, TokenName

_TOKEN = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z0-9_-]+)*$")


class ObservabilityConfig(StrictModel):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    trace_sample_rate: Annotated[float, Field(ge=0, le=1)] = 1.0
    retain_days: Annotated[int, Field(ge=1, le=365)] = 14


class CapabilityModelRoutes(StrictModel):
    vision: TokenName | None = None
    image_generation: TokenName | None = None
    video_generation: TokenName | None = None


class VoiceAsrConfig(StrictModel):
    """语音识别提供方（docs/33）：MiMo 云端或 faster-whisper 本地转写。"""

    provider: Literal["mimo", "faster_whisper"] = "mimo"
    model: Annotated[str, Field(min_length=1, max_length=200)] = "mimo-v2.5-asr"
    base_url: AnyHttpUrl | None = AnyHttpUrl("https://api.xiaomimimo.com/v1")
    secret_ref: Annotated[str, Field(pattern=r"^env:[A-Z][A-Z0-9_]{2,127}$")] | None = None
    secret_value: Annotated[str, Field(max_length=1024)] | None = None
    language: Literal["auto", "zh", "en"] = "auto"
    device: Literal["auto", "cpu", "cuda"] = "auto"
    compute_type: Annotated[str, Field(min_length=1, max_length=64)] = "default"
    initial_prompt: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    hotwords: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    runs_local: bool = False

    @model_validator(mode="after")
    def provider_requirements(self) -> VoiceAsrConfig:
        if self.provider == "mimo":
            if self.initial_prompt is not None or self.hotwords is not None:
                raise ValueError(
                    "initial_prompt and hotwords are only supported by faster_whisper"
                )
            if self.base_url is None:
                raise ValueError("mimo voice asr requires base_url")
            if self.secret_value is None and self.secret_ref is None:
                raise ValueError("mimo voice asr requires secret_value or secret_ref")
            if self.runs_local:
                raise ValueError("mimo voice asr cannot be marked local")
        elif not self.runs_local:
            raise ValueError("faster_whisper voice asr must run locally")
        return self


class VoiceTtsProviderConfig(StrictModel):
    """语音合成提供方：mimo（PCM 直出）为主、edge_tts 免费兜底。"""

    provider: Literal["mimo", "edge_tts"]
    model: Annotated[str, Field(min_length=1, max_length=200)] = "mimo-v2.5-tts"
    base_url: AnyHttpUrl | None = None
    secret_ref: Annotated[str, Field(pattern=r"^env:[A-Z][A-Z0-9_]{2,127}$")] | None = None
    secret_value: Annotated[str, Field(max_length=1024)] | None = None
    voice: Annotated[str, Field(min_length=1, max_length=100)] = "冰糖"
    enabled: bool = True
    runs_local: bool = False

    @model_validator(mode="after")
    def provider_specific_requirements(self) -> VoiceTtsProviderConfig:
        if self.provider == "mimo":
            if self.base_url is None:
                raise ValueError("mimo tts requires base_url")
            if self.secret_value is None and self.secret_ref is None:
                raise ValueError("mimo tts requires secret_value or secret_ref")
        return self


class VoiceConfig(StrictModel):
    """语音管线配置：ASR 单选 + 有序 TTS 故障转移链（docs/33）。"""

    asr: VoiceAsrConfig | None = None
    tts: Annotated[list[VoiceTtsProviderConfig], Field(max_length=4)] = Field(default_factory=list)


class QueryToolConfig(StrictModel):
    enabled: bool = True
    weather_enabled: bool = True
    nearby_enabled: bool = True
    route_enabled: bool = True
    default_city: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    precise_location_policy: Literal["ask_each_time", "allow_session"] = "ask_each_time"


class AmapToolConfig(StrictModel):
    enabled: bool = False
    base_url: AnyHttpUrl = AnyHttpUrl("https://restapi.amap.com")
    secret_ref: Annotated[str, Field(pattern=r"^env:[A-Z][A-Z0-9_]{2,127}$")] | None = None
    secret_value: Annotated[str, Field(max_length=1024)] | None = None
    timeout_ms: Annotated[int, Field(ge=500, le=30_000)] = 3_500
    max_retries: Annotated[int, Field(ge=0, le=2)] = 1
    max_concurrency: Annotated[int, Field(ge=1, le=16)] = 2
    requests_per_minute: Annotated[int, Field(ge=1, le=10_000)] = 30

    @model_validator(mode="after")
    def validate_provider(self) -> AmapToolConfig:
        if self.enabled and self.secret_ref is None and self.secret_value is None:
            raise ValueError("enabled amap provider requires secret_ref or secret_value")
        if self.enabled and str(self.base_url).rstrip("/") != "https://restapi.amap.com":
            raise ValueError("production amap base_url must use the fixed HTTPS host")
        return self


class ScreenAwarenessConfig(StrictModel):
    """中枢周期截屏感知：设备端仅保留 TCC/锁屏/隐私暂停三道硬闸门。"""

    enabled: bool = False
    interval_seconds: Annotated[int, Field(ge=15, le=600)] = 60
    displays: Annotated[list[Annotated[int, Field(ge=1, le=32)]], Field(max_length=8)] = Field(
        default_factory=lambda: [1]
    )
    analysis_prompt: Annotated[str, Field(min_length=1, max_length=1_000)] = (
        "概括这块屏幕当前展示的主要内容，并判断是否值得主动分享或记忆。"
    )
    memory_enabled: bool = True
    proactive_enabled: bool = True
    unchanged_skip_threshold: Annotated[int, Field(ge=0, le=64)] = 6

    @model_validator(mode="after")
    def validate_displays(self) -> ScreenAwarenessConfig:
        if self.enabled and not self.displays:
            raise ValueError("enabled screen awareness requires at least one display index")
        return self


class ToolsConfig(StrictModel):
    enabled: bool = False
    max_tool_rounds: Literal[1] = 1
    query: QueryToolConfig = Field(default_factory=QueryToolConfig)
    amap: AmapToolConfig = Field(default_factory=AmapToolConfig)

    @model_validator(mode="after")
    def validate_provider(self) -> ToolsConfig:
        if self.enabled and self.query.enabled and not self.amap.enabled:
            raise ValueError("enabled query tools require an enabled amap provider")
        return self


class HomeAssistantProactiveRuleConfig(StrictModel):
    rule_id: TokenName
    kind: Literal[
        "water_leak",
        "temperature_high",
        "temperature_low",
        "humidity_high",
        "humidity_low",
        "pm25_high",
        "light_on_too_long",
        "device_offline",
    ]
    enabled: bool = False
    threshold: Annotated[float, Field(ge=-100, le=10_000)] | None = None
    duration_seconds: Annotated[int, Field(ge=0, le=86_400)] = 300
    cooldown_minutes: Annotated[int, Field(ge=1, le=10_080)] = 240
    message: Annotated[str, Field(min_length=1, max_length=500)] | None = None

    @model_validator(mode="after")
    def validate_threshold(self) -> HomeAssistantProactiveRuleConfig:
        threshold_kinds = {
            "temperature_high",
            "temperature_low",
            "humidity_high",
            "humidity_low",
            "pm25_high",
        }
        if self.kind in threshold_kinds and self.threshold is None:
            raise ValueError("home assistant proactive threshold is required")
        return self


def _default_home_assistant_proactive_rules() -> list[HomeAssistantProactiveRuleConfig]:
    return [
        HomeAssistantProactiveRuleConfig(
            rule_id="device_offline",
            kind="device_offline",
            enabled=True,
            duration_seconds=120,
            cooldown_minutes=240,
        )
    ]


class HomeAssistantEntityConfig(StrictModel):
    entity_id: Annotated[
        str,
        Field(
            min_length=3,
            max_length=255,
            pattern=r"^[a-z0-9_]+\.[a-z0-9_]+$",
        ),
    ]
    display_name: Annotated[str, Field(min_length=1, max_length=160)]
    aliases: Annotated[list[str], Field(max_length=16)] = Field(default_factory=list)
    read_allowed: bool = False
    history_allowed: bool = False
    history_max_hours: Annotated[int, Field(ge=1, le=168)] = 24
    allowed_actions: Annotated[
        list[Literal["turn_on", "turn_off", "toggle", "set_temperature"]],
        Field(max_length=4),
    ] = Field(default_factory=list)
    confirmation_required_actions: Annotated[
        list[Literal["turn_on", "turn_off", "toggle", "set_temperature"]],
        Field(max_length=4),
    ] = Field(default_factory=list)
    proactive_rules: Annotated[list[HomeAssistantProactiveRuleConfig], Field(max_length=16)] = (
        Field(default_factory=_default_home_assistant_proactive_rules)
    )
    privacy_level: Literal["L0", "L1", "L2", "L3"] = "L1"
    allowed_attributes: Annotated[list[str], Field(max_length=32)] = Field(
        default_factory=lambda: [
            "friendly_name",
            "device_class",
            "unit_of_measurement",
        ]
    )

    @model_validator(mode="after")
    def validate_names(self) -> HomeAssistantEntityConfig:
        normalized = [value.strip().casefold() for value in self.aliases]
        if any(not value for value in normalized):
            raise ValueError("home assistant aliases cannot be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("home assistant aliases must be unique")
        if self.display_name.strip().casefold() in normalized:
            raise ValueError("display_name cannot be repeated as an alias")
        if len(self.allowed_actions) != len(set(self.allowed_actions)):
            raise ValueError("home assistant allowed actions must be unique")
        if len(self.confirmation_required_actions) != len(set(self.confirmation_required_actions)):
            raise ValueError("home assistant confirmation actions must be unique")
        if not set(self.confirmation_required_actions).issubset(self.allowed_actions):
            raise ValueError("home assistant confirmation actions must be allowed")
        rule_ids = [rule.rule_id for rule in self.proactive_rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("home assistant proactive rule ids must be unique per entity")
        domain = self.entity_id.split(".", 1)[0]
        domain_actions = {
            "light": {"turn_on", "turn_off", "toggle"},
            "switch": {"turn_on", "turn_off", "toggle"},
            "climate": {"turn_on", "turn_off", "set_temperature"},
        }
        if not set(self.allowed_actions).issubset(domain_actions.get(domain, set())):
            raise ValueError("home assistant action is not valid for entity domain")
        return self


class HomeAssistantConfig(StrictModel):
    enabled: bool = False
    instance_id: Annotated[str, Field(min_length=1, max_length=80)] = "home-main"
    base_url: AnyHttpUrl | None = None
    secret_ref: Annotated[str, Field(pattern=r"^env:[A-Z][A-Z0-9_]{2,127}$")] | None = None
    secret_value: Annotated[str, Field(max_length=4096)] | None = None
    verify_tls: bool = True
    allow_insecure_local_http: bool = False
    connect_timeout_ms: Annotated[int, Field(ge=500, le=30_000)] = 5_000
    request_timeout_ms: Annotated[int, Field(ge=500, le=30_000)] = 8_000
    reconnect_min_seconds: Annotated[float, Field(ge=0.1, le=30)] = 1
    reconnect_max_seconds: Annotated[float, Field(ge=1, le=300)] = 30
    state_cache_ttl_seconds: Annotated[int, Field(ge=5, le=86_400)] = 300
    proactive_enabled: bool = False
    proactive_quiet_hours_start: Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")] = (
        "23:00"
    )
    proactive_quiet_hours_end: Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")] = (
        "07:00"
    )
    proactive_daily_limit: Annotated[int, Field(ge=1, le=50)] = 5
    proactive_critical_bypasses_quiet_hours: bool = True
    entities: Annotated[list[HomeAssistantEntityConfig], Field(max_length=4096)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_connection(self) -> HomeAssistantConfig:
        if self.enabled and (
            self.base_url is None or (self.secret_ref is None and self.secret_value is None)
        ):
            raise ValueError(
                "enabled home assistant requires base_url and secret_ref or secret_value"
            )
        if self.reconnect_max_seconds < self.reconnect_min_seconds:
            raise ValueError("home assistant reconnect maximum must be >= minimum")
        if self.base_url is not None and self.base_url.path not in {"", "/"}:
            raise ValueError("home assistant base_url cannot contain a path")
        if self.base_url is not None and self.base_url.scheme == "http":
            if not self.allow_insecure_local_http:
                raise ValueError("insecure home assistant HTTP requires explicit opt-in")
            host = self.base_url.host or ""
            is_local_name = host in {"localhost", "homeassistant"} or host.endswith(".local")
            try:
                is_private_ip = ip_address(host).is_private
            except ValueError:
                is_private_ip = False
            if not is_local_name and not is_private_ip:
                raise ValueError("insecure home assistant HTTP is restricted to local hosts")
        entity_ids = [item.entity_id for item in self.entities]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("home assistant entity_id values must be unique")
        names: list[str] = []
        for item in self.entities:
            names.append(item.display_name.strip().casefold())
            names.extend(value.strip().casefold() for value in item.aliases)
        if len(names) != len(set(names)):
            raise ValueError("home assistant display names and aliases must be unique")
        return self


class IntegrationsConfig(StrictModel):
    home_assistant: HomeAssistantConfig = Field(default_factory=HomeAssistantConfig)


class ProactiveChannelConfig(StrictModel):
    enabled: bool = False
    priority: Annotated[int, Field(ge=1, le=100)] = 50
    max_privacy_level: Literal["L0", "L1", "L2"] = "L1"
    critical_only: bool = False


class ProactiveOutputConfig(StrictModel):
    enabled: bool = True
    delivery_mode: Literal["first_available", "all_enabled"] = "all_enabled"
    web_chat: ProactiveChannelConfig = Field(
        default_factory=lambda: ProactiveChannelConfig(enabled=True, priority=100)
    )
    desktop_notification: ProactiveChannelConfig = Field(
        default_factory=lambda: ProactiveChannelConfig(priority=80)
    )
    voice: ProactiveChannelConfig = Field(
        default_factory=lambda: ProactiveChannelConfig(priority=60)
    )

    @model_validator(mode="after")
    def require_enabled_channel(self) -> ProactiveOutputConfig:
        if self.enabled and not any(
            channel.enabled
            for channel in (self.web_chat, self.desktop_notification, self.voice)
        ):
            raise ValueError("enabled proactive output requires at least one channel")
        if self.desktop_notification.max_privacy_level == "L2":
            raise ValueError("desktop notifications cannot carry L2 content")
        return self


class HubConfig(StrictModel):
    schema_version: Literal[1] = 1
    models: Annotated[dict[str, ModelEndpoint], Field(min_length=1, max_length=64)]
    routes: dict[LLMRoute, RoutePolicy]
    capability_models: CapabilityModelRoutes = Field(default_factory=CapabilityModelRoutes)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    integrations: IntegrationsConfig = Field(default_factory=IntegrationsConfig)
    proactive_output: ProactiveOutputConfig = Field(default_factory=ProactiveOutputConfig)
    screen_awareness: ScreenAwarenessConfig = Field(default_factory=ScreenAwarenessConfig)

    @model_validator(mode="before")
    @classmethod
    def _drop_deprecated_fields(cls, data: Any) -> Any:
        # 兼容已入库的旧配置: 废弃的 llm 全局字段不再使用,
        # 由模型级 max_tokens 完全接管。
        if isinstance(data, dict):
            data.pop("llm", None)
        return data

    @model_validator(mode="after")
    def validate_routes(self) -> HubConfig:
        invalid_names = [name for name in self.models if _TOKEN.fullmatch(name) is None]
        if invalid_names:
            raise ValueError("model endpoint names must be tokens")
        if set(self.routes) != set(LLMRoute):
            raise ValueError("dialogue, utility and private routes are required")
        for route, policy in self.routes.items():
            for endpoint_name in [policy.primary, *policy.fallbacks]:
                endpoint = self.models.get(endpoint_name)
                if endpoint is None:
                    raise ValueError(f"route references unknown model endpoint: {endpoint_name}")
                if not endpoint.enabled:
                    raise ValueError(f"route references disabled model endpoint: {endpoint_name}")
                if ModelKind(endpoint.kind) is not ModelKind.TEXT:
                    raise ValueError("dialogue, utility and private routes require text models")
                if route == LLMRoute.PRIVATE and not endpoint.runs_local:
                    raise ValueError("private route cannot reference cloud models")

        capability_kinds = {
            "vision": ModelKind.VISION,
            "image_generation": ModelKind.IMAGE_GENERATION,
            "video_generation": ModelKind.VIDEO_GENERATION,
        }
        for field_name, expected_kind in capability_kinds.items():
            endpoint_name = getattr(self.capability_models, field_name)
            if endpoint_name is None:
                continue
            endpoint = self.models.get(endpoint_name)
            if endpoint is None:
                raise ValueError(f"capability references unknown model endpoint: {endpoint_name}")
            if not endpoint.enabled:
                raise ValueError(f"capability references disabled model endpoint: {endpoint_name}")
            if ModelKind(endpoint.kind) is not expected_kind:
                raise ValueError(f"capability {field_name} requires a {expected_kind.value} model")
        return self
