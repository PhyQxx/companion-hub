"""Public exports resolved without importing unrelated adapters."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .service import CommutePlan, CommuteRouteError, CommuteService
    from .tools import CommuteCheckTool

_EXPORTS = {
    "CommutePlan": ("app.commute.service", "CommutePlan"),
    "CommuteRouteError": ("app.commute.service", "CommuteRouteError"),
    "CommuteService": ("app.commute.service", "CommuteService"),
    "CommuteCheckTool": ("app.commute.tools", "CommuteCheckTool"),
}


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "CommuteCheckTool",
    "CommutePlan",
    "CommuteRouteError",
    "CommuteService",
]
