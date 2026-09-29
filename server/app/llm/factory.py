from __future__ import annotations

import hashlib
from collections import OrderedDict
from collections.abc import Callable

from app.config.models import HubConfig
from app.observability import TraceRecorder

from .contracts import ModelKind
from .provider import EnvSecretProvider, LiteLLMProvider, LLMProvider
from .router import LLMRouter

RouterBuilder = Callable[[HubConfig], LLMRouter]


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


class CachedRouterBuilder:
    """按配置指纹缓存 Router（PERE-01，docs/09 §8）。

    Provider/Router 每次补全独立发起 HTTP 调用、无会话状态，跨轮复用
    只省构建开销；指纹取配置 JSON 的 sha256，Admin 发布新配置自动失效。
    单进程内有效（uvicorn 单 worker 部署形态），容量 4 防泄漏。
    """

    def __init__(
        self,
        secrets: EnvSecretProvider,
        *,
        capacity: int = 4,
        traces: TraceRecorder | None = None,
    ) -> None:
        self._secrets = secrets
        self._traces = traces
        self._capacity = capacity
        self._cache: OrderedDict[str, LLMRouter] = OrderedDict()

    def __call__(self, config: HubConfig) -> LLMRouter:
        key = hashlib.sha256(config.model_dump_json().encode()).hexdigest()
        router = self._cache.get(key)
        if router is not None:
            self._cache.move_to_end(key)
            return router
        router = build_router(config, self._secrets, traces=self._traces)
        self._cache[key] = router
        while len(self._cache) > self._capacity:
            self._cache.popitem(last=False)
        return router


async def validate_provider_connectivity(
    config: HubConfig,
    secrets: EnvSecretProvider,
) -> None:
    for provider in build_providers(config, secrets).values():
        await provider.probe()
