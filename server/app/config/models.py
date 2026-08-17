from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.llm.contracts import LLMRoute, ModelEndpoint, RoutePolicy
from app.schemas.common import StrictModel

_TOKEN = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z0-9_-]+)*$")


class ObservabilityConfig(StrictModel):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    trace_sample_rate: Annotated[float, Field(ge=0, le=1)] = 1.0
    retain_days: Annotated[int, Field(ge=1, le=365)] = 14


class HubConfig(StrictModel):
    schema_version: Literal[1] = 1
    models: Annotated[dict[str, ModelEndpoint], Field(min_length=1, max_length=64)]
    routes: dict[LLMRoute, RoutePolicy]
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
                if route == LLMRoute.PRIVATE and not endpoint.runs_local:
                    raise ValueError("private route cannot reference cloud models")
        return self
