from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, model_validator

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


class HubConfig(StrictModel):
    schema_version: Literal[1] = 1
    models: Annotated[dict[str, ModelEndpoint], Field(min_length=1, max_length=64)]
    routes: dict[LLMRoute, RoutePolicy]
    capability_models: CapabilityModelRoutes = Field(default_factory=CapabilityModelRoutes)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

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
