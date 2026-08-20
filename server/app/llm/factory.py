from __future__ import annotations

from app.config.models import HubConfig
from app.observability import TraceRecorder

from .contracts import ModelKind
from .provider import EnvSecretProvider, LiteLLMProvider, LLMProvider
from .router import LLMRouter


def build_providers(
    config: HubConfig,
    secrets: EnvSecretProvider,
) -> dict[str, LLMProvider]:
    providers: dict[str, LLMProvider] = {}
    for name, endpoint in config.models.items():
        if not endpoint.enabled:
            continue
        if ModelKind(endpoint.kind) is not ModelKind.TEXT:
            continue
        if endpoint.provider != "openai_compatible":
            raise ValueError(f"unsupported LLM provider type: {endpoint.provider}")
        providers[name] = LiteLLMProvider(name, endpoint, secrets)
    return providers


def build_router(
    config: HubConfig,
    secrets: EnvSecretProvider,
    *,
    traces: TraceRecorder | None = None,
) -> LLMRouter:
    return LLMRouter(
        endpoints=config.models,
        routes=config.routes,
        providers=build_providers(config, secrets),
        traces=traces,
    )


async def validate_provider_connectivity(
    config: HubConfig,
    secrets: EnvSecretProvider,
) -> None:
    for provider in build_providers(config, secrets).values():
        await provider.probe()
