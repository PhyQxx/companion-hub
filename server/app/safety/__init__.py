"""Public safety exports; implementations load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .activity import ActivityTracker, SafetyActivityScheduler
    from .service import SafetyAlertService
    from .store import SafetyAlertStore

_EXPORTS = {
    "ActivityTracker": ("app.safety.activity", "ActivityTracker"),
    "SafetyActivityScheduler": ("app.safety.activity", "SafetyActivityScheduler"),
    "SafetyAlertService": ("app.safety.service", "SafetyAlertService"),
    "SafetyAlertStore": ("app.safety.store", "SafetyAlertStore"),
}

__all__ = ["ActivityTracker", "SafetyActivityScheduler", "SafetyAlertService", "SafetyAlertStore"]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
