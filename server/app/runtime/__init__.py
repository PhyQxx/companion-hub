"""Public runtime exports; implementations load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .lease import LeaseManager, LeaseResult
    from .turn_coordinator import TurnContext, TurnCoordinator, TurnState

_EXPORTS = {
    "LeaseManager": ("app.runtime.lease", "LeaseManager"),
    "LeaseResult": ("app.runtime.lease", "LeaseResult"),
    "TurnContext": ("app.runtime.turn_coordinator", "TurnContext"),
    "TurnCoordinator": ("app.runtime.turn_coordinator", "TurnCoordinator"),
    "TurnState": ("app.runtime.turn_coordinator", "TurnState"),
}

__all__ = [
    "LeaseManager",
    "LeaseResult",
    "TurnContext",
    "TurnCoordinator",
    "TurnState",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
