"""Public exports resolved without importing unrelated adapters."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .amap import AmapProvider, AmapProviderError, wgs84_to_gcj02
    from .contracts import ToolContext, ToolExecution, ToolHandler, ToolResult
    from .desktop import DesktopNotifyArgs, DesktopNotifyTool
    from .executor import ToolExecutor
    from .factory import QueryToolRuntime, build_query_tool_runtime
    from .intent import select_device_tools, select_query_tools, supports_device_capability
    from .ledger import ToolLedger, ToolLedgerEntry
    from .locate import GetLocationArgs, LocationTool, location_tool_definition
    from .location import ClientLocation, ClientLocationPayload, LocationSource, ResolvedLocation
    from .nearby import NearbyTool, SearchNearbyArgs, nearby_tool_definition
    from .registry import ToolRegistry
    from .route import PlanRouteArgs, RouteTool, route_tool_definition
    from .weather import GetWeatherArgs, WeatherTool, weather_tool_definition
    from .webfetch import FetchWebpageTool

_EXPORTS = {
    "AmapProvider": ("app.tools.amap", "AmapProvider"),
    "AmapProviderError": ("app.tools.amap_models", "AmapProviderError"),
    "wgs84_to_gcj02": ("app.tools.amap", "wgs84_to_gcj02"),
    "ToolContext": ("app.tools.contracts", "ToolContext"),
    "ToolExecution": ("app.tools.contracts", "ToolExecution"),
    "ToolHandler": ("app.tools.contracts", "ToolHandler"),
    "ToolResult": ("app.tools.contracts", "ToolResult"),
    "DesktopNotifyArgs": ("app.tools.desktop", "DesktopNotifyArgs"),
    "DesktopNotifyTool": ("app.tools.desktop", "DesktopNotifyTool"),
    "ToolExecutor": ("app.tools.executor", "ToolExecutor"),
    "QueryToolRuntime": ("app.tools.factory", "QueryToolRuntime"),
    "build_query_tool_runtime": ("app.tools.factory", "build_query_tool_runtime"),
    "select_device_tools": ("app.tools.intent", "select_device_tools"),
    "select_query_tools": ("app.tools.intent", "select_query_tools"),
    "supports_device_capability": ("app.tools.intent", "supports_device_capability"),
    "ToolLedger": ("app.tools.ledger", "ToolLedger"),
    "ToolLedgerEntry": ("app.tools.ledger", "ToolLedgerEntry"),
    "GetLocationArgs": ("app.tools.locate", "GetLocationArgs"),
    "LocationTool": ("app.tools.locate", "LocationTool"),
    "location_tool_definition": ("app.tools.locate", "location_tool_definition"),
    "ClientLocation": ("app.tools.location", "ClientLocation"),
    "ClientLocationPayload": ("app.tools.location", "ClientLocationPayload"),
    "LocationSource": ("app.tools.location", "LocationSource"),
    "ResolvedLocation": ("app.tools.location", "ResolvedLocation"),
    "NearbyTool": ("app.tools.nearby", "NearbyTool"),
    "SearchNearbyArgs": ("app.tools.nearby", "SearchNearbyArgs"),
    "nearby_tool_definition": ("app.tools.nearby", "nearby_tool_definition"),
    "ToolRegistry": ("app.tools.registry", "ToolRegistry"),
    "PlanRouteArgs": ("app.tools.route", "PlanRouteArgs"),
    "RouteTool": ("app.tools.route", "RouteTool"),
    "route_tool_definition": ("app.tools.route", "route_tool_definition"),
    "GetWeatherArgs": ("app.tools.weather", "GetWeatherArgs"),
    "WeatherTool": ("app.tools.weather", "WeatherTool"),
    "weather_tool_definition": ("app.tools.weather", "weather_tool_definition"),
    "FetchWebpageTool": ("app.tools.webfetch", "FetchWebpageTool"),
}


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "AmapProvider",
    "AmapProviderError",
    "ClientLocation",
    "ClientLocationPayload",
    "DesktopNotifyArgs",
    "DesktopNotifyTool",
    "FetchWebpageTool",
    "GetLocationArgs",
    "GetWeatherArgs",
    "LocationSource",
    "LocationTool",
    "NearbyTool",
    "PlanRouteArgs",
    "QueryToolRuntime",
    "ResolvedLocation",
    "RouteTool",
    "SearchNearbyArgs",
    "ToolContext",
    "ToolExecution",
    "ToolExecutor",
    "ToolHandler",
    "ToolLedger",
    "ToolLedgerEntry",
    "ToolRegistry",
    "ToolResult",
    "WeatherTool",
    "build_query_tool_runtime",
    "location_tool_definition",
    "nearby_tool_definition",
    "route_tool_definition",
    "select_device_tools",
    "select_query_tools",
    "supports_device_capability",
    "weather_tool_definition",
    "wgs84_to_gcj02",
]
