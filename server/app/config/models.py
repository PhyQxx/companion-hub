# ruff: noqa: RUF002
from __future__ import annotations

import re
from typing import Annotated, Literal

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
    secret_ref: Annotated[
        str, Field(pattern=r"^env:[A-Z][A-Z0-9_]{2,127}$")
    ] | None = None
    secret_value: Annotated[str, Field(max_length=1024)] | None = None
    language: Literal["auto", "zh", "en"] = "auto"
    device: Literal["auto", "cpu", "cuda"] = "auto"
    compute_type: Annotated[str, Field(min_length=1, max_length=64)] = "default"
    runs_local: bool = False

    @model_validator(mode="after")
    def provider_requirements(self) -> VoiceAsrConfig:
        if self.provider == "mimo":
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
    secret_ref: Annotated[
        str, Field(pattern=r"^env:[A-Z][A-Z0-9_]{2,127}$")
    ] | None = None
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
    tts: Annotated[list[VoiceTtsProviderConfig], Field(max_length=4)] = Field(
        default_factory=list
    )


class HubConfig(StrictModel):
    schema_version: Literal[1] = 1
    models: Annotated[dict[str, ModelEndpoint], Field(min_length=1, max_length=64)]
    routes: dict[LLMRoute, RoutePolicy]
    capability_models: CapabilityModelRoutes = Field(default_factory=CapabilityModelRoutes)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)

    @model_validator(mode="before")
    @classmethod
    def _drop_deprecated_fields(cls, data: dict) -> dict:
        # 兼容已入库的旧配置：废弃的 llm 全局字段不再使用，
        # 由模型级 max_tokens 完全接管。
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
                raise ValueError(
                    f"capability {field_name} requires a {expected_kind.value} model"
                )
        return self
