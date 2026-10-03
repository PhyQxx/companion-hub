"""Parse detached route payloads without loading HTTP/provider implementations."""

from typing import Any

from .amap_models import AmapProviderError


def _integer(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_route(payload: dict[str, Any], mode: str) -> tuple[int, int | None, list[str]]:
    route = payload.get("route")
    if not isinstance(route, dict):
        raise AmapProviderError("route_unavailable")
    choices = route.get("transits" if mode == "transit" else "paths")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise AmapProviderError("route_unavailable")
    choice = choices[0]
    distance = _integer(choice.get("distance"))
    if distance is None:
        raise AmapProviderError("tool_result_invalid")
    cost = choice.get("cost")
    duration = _integer(cost.get("duration")) if isinstance(cost, dict) else None
    if duration is None:
        duration = _integer(choice.get("duration"))
    steps: list[str] = []
    raw_steps = choice.get("steps")
    if isinstance(raw_steps, list):
        steps = [
            str(item["instruction"])
            for item in raw_steps[:12]
            if isinstance(item, dict) and item.get("instruction")
        ]
    return distance, duration, steps
