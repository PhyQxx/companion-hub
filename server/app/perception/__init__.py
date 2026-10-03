"""Public perception exports; implementations load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .models import (
        PerceptionDisposition,
        PerceptionResult,
        ProactivePolicySettings,
        SemanticEventAuditView,
    )
    from .pipeline import HandleResult, PerceptionPipeline, ValidateEvent
    from .policy import CRITICAL_EVENTS, ProactivePolicy
    from .store import PerceptionStore

_EXPORTS = {
    "CRITICAL_EVENTS": ("app.perception.policy", "CRITICAL_EVENTS"),
    "HandleResult": ("app.perception.pipeline", "HandleResult"),
    "PerceptionDisposition": ("app.perception.models", "PerceptionDisposition"),
    "PerceptionPipeline": ("app.perception.pipeline", "PerceptionPipeline"),
    "PerceptionResult": ("app.perception.models", "PerceptionResult"),
    "PerceptionStore": ("app.perception.store", "PerceptionStore"),
    "ProactivePolicy": ("app.perception.policy", "ProactivePolicy"),
    "ProactivePolicySettings": ("app.perception.models", "ProactivePolicySettings"),
    "SemanticEventAuditView": ("app.perception.models", "SemanticEventAuditView"),
    "ValidateEvent": ("app.perception.pipeline", "ValidateEvent"),
}

__all__ = [
    "CRITICAL_EVENTS",
    "HandleResult",
    "PerceptionDisposition",
    "PerceptionPipeline",
    "PerceptionResult",
    "PerceptionStore",
    "ProactivePolicy",
    "ProactivePolicySettings",
    "SemanticEventAuditView",
    "ValidateEvent",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
