from __future__ import annotations

from dataclasses import dataclass

from app.config import HubConfig
from app.llm.provider import EnvSecretProvider

from .amap import AmapProvider
from .contracts import ToolHandler
from .executor import ToolExecutor
from .nearby import NearbyTool
from .registry import ToolRegistry
from .route import RouteTool
from .weather import WeatherTool


@dataclass(slots=True)
class QueryToolRuntime:
    executor: ToolExecutor
    provider: AmapProvider

    async def close(self) -> None:
        await self.provider.close()


def build_query_tool_runtime(
    config: HubConfig,
    secrets: EnvSecretProvider,
) -> QueryToolRuntime:
    amap = config.tools.amap
    if not config.tools.enabled or not amap.enabled:
        raise ValueError("query tools are disabled")
    if amap.secret_value is not None:
        api_key = amap.secret_value
    elif amap.secret_ref is not None:
        api_key = secrets.resolve(amap.secret_ref)
    else:
        raise ValueError("amap secret is unavailable")
    provider = AmapProvider(
        api_key,
        base_url=str(amap.base_url).rstrip("/"),
        timeout_ms=amap.timeout_ms,
        max_retries=amap.max_retries,
        max_concurrency=amap.max_concurrency,
        requests_per_minute=amap.requests_per_minute,
    )
    handlers: list[ToolHandler] = []
    if config.tools.query.weather_enabled:
        handlers.append(WeatherTool(provider))
    if config.tools.query.nearby_enabled:
        handlers.append(NearbyTool(provider))
    if config.tools.query.route_enabled:
        handlers.append(RouteTool(provider))
    return QueryToolRuntime(
        executor=ToolExecutor(ToolRegistry(handlers)),
        provider=provider,
    )
