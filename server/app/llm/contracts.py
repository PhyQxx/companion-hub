from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AnyHttpUrl, Field, model_validator

from app.schemas import PrivacyLevel
from app.schemas.common import StrictModel, TokenName


class LLMRoute(StrEnum):
    DIALOGUE = "dialogue"
    VOICE = "voice"
    UTILITY = "utility"
    PRIVATE = "private"


class ModelKind(StrEnum):
    TEXT = "text"
    VISION = "vision"
    IMAGE_GENERATION = "image_generation"
    VIDEO_GENERATION = "video_generation"


class ToolFunction(StrictModel):
    name: TokenName
    arguments: dict[str, Any]


class ToolCall(StrictModel):
    id: Annotated[str, Field(min_length=1, max_length=200)]
    type: Literal["function"] = "function"
    function: ToolFunction


class ToolDefinition(StrictModel):
    type: Literal["function"] = "function"
    name: TokenName
    description: Annotated[str, Field(min_length=1, max_length=2_000)]
    parameters: dict[str, Any]


class LLMMessage(StrictModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: Annotated[str, Field(max_length=1_000_000)] = ""
    tool_call_id: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    name: TokenName | None = None
    tool_calls: Annotated[list[ToolCall], Field(max_length=8)] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_role_fields(self) -> LLMMessage:
        if self.role in {"system", "user"} and not self.content:
            raise ValueError("system and user messages require content")
        if self.role == "tool" and (self.tool_call_id is None or self.name is None):
            raise ValueError("tool messages require tool_call_id and name")
        if self.role != "assistant" and self.tool_calls:
            raise ValueError("only assistant messages can contain tool_calls")
        return self


class CompletionRequest(StrictModel):
    trace_id: UUID
    messages: Annotated[list[LLMMessage], Field(min_length=1, max_length=256)]
    privacy_level: PrivacyLevel
    route: LLMRoute
    max_tokens: Annotated[int, Field(gt=0, le=131_072)] = 1_024
    temperature: Annotated[float, Field(ge=0, le=2)] = 0.7
    json_mode: bool = False
    tools: Annotated[list[ToolDefinition], Field(max_length=16)] = Field(default_factory=list)
    tool_choice: Literal["auto", "none"] = "auto"


class ModelUsage(StrictModel):
    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0
    total_tokens: Annotated[int, Field(ge=0)] = 0
    estimated_cost: Annotated[float, Field(ge=0)] = 0


class CompletionResult(StrictModel):
    text: str
    provider: TokenName
    model: str
    endpoint: TokenName
    route: LLMRoute
    request_id: str | None = None
    finish_reason: str | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)
    latency_ms: Annotated[float, Field(ge=0)]
    tool_calls: Annotated[list[ToolCall], Field(max_length=8)] = Field(default_factory=list)


class ModelEndpoint(StrictModel):
    enabled: bool = True
    kind: ModelKind = ModelKind.TEXT
    provider: TokenName
    model: Annotated[str, Field(min_length=1, max_length=200)]
    supports_json_mode: bool = False
    supports_tool_calling: bool = False
    thinking_mode: Literal["provider_default", "enabled", "disabled"] = "provider_default"
    # 思考型模型的隐藏推理开销：线上 max_tokens 在请求预算之上叠加该值。
    # Qwen3 类模型在 OpenAI-compatible 路径下 reasoning 与正文共享输出预算，
    # 不叠加时 512 token 会被推理耗尽，message.content 恒为空。
    reasoning_overhead_tokens: Annotated[int, Field(ge=0, le=131_072)] = 0
    base_url: AnyHttpUrl
    secret_ref: Annotated[
        str, Field(pattern=r"^env:[A-Z][A-Z0-9_]{2,127}$")
    ] | None = None
    secret_value: Annotated[str, Field(max_length=1024)] | None = None
    runs_local: bool
    max_privacy_level: PrivacyLevel
    timeout_ms: Annotated[int, Field(ge=100, le=120_000)] = 12_000
    max_retries: Annotated[int, Field(ge=0, le=3)] = 1
    max_tokens: Annotated[int, Field(gt=0, le=131_072)] | None = None
    max_context_tokens: Annotated[int, Field(gt=0)] = 131_072
    input_cost_per_million: Annotated[float, Field(ge=0)] = 0
    output_cost_per_million: Annotated[float, Field(ge=0)] = 0


class RoutePolicy(StrictModel):
    primary: TokenName
    fallbacks: Annotated[list[TokenName], Field(max_length=8)] = Field(default_factory=list)
    # 非流式调用的总时长上限；流式调用里只约束「首个 chunk 到达前」的等待。
    timeout_ms: Annotated[int, Field(ge=100, le=120_000)] | None = None
    # 流式首 chunk 看门狗：超过该时限仍无任何输出即判超时并降级；缺省回退 timeout_ms。
    stream_first_chunk_timeout_ms: Annotated[int, Field(ge=100, le=120_000)] | None = None
    # 流式空闲看门狗：出字后相邻 chunk 的最大间隔，超时视为流中断；缺省 10s。
    # 出字后不再限制总时长——模型持续产出时不允许被掐断（语音回合已播出的话无法收回）。
    stream_idle_timeout_ms: Annotated[int, Field(ge=100, le=120_000)] | None = None

    @model_validator(mode="after")
    def endpoints_are_unique(self) -> RoutePolicy:
        if self.primary in self.fallbacks or len(set(self.fallbacks)) != len(self.fallbacks):
            raise ValueError("route endpoints must be unique")
        return self
