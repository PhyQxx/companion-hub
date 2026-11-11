"""Public output exports; implementations load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .proactive import (
        ProactiveChannelAttempt,
        ProactiveDeliveryResult,
        ProactiveDeliveryService,
    )
    from .routing import (
        EndpointRegistration,
        NoCompatibleOutput,
        OutputRouter,
        intersect_capabilities,
    )

_EXPORTS = {
    "EndpointRegistration": ("app.output.routing", "EndpointRegistration"),
    "NoCompatibleOutput": ("app.output.routing", "NoCompatibleOutput"),
    "OutputRouter": ("app.output.routing", "OutputRouter"),
    "ProactiveChannelAttempt": ("app.output.proactive", "ProactiveChannelAttempt"),
    "ProactiveDeliveryResult": ("app.output.proactive", "ProactiveDeliveryResult"),
    "ProactiveDeliveryService": ("app.output.proactive", "ProactiveDeliveryService"),
    "intersect_capabilities": ("app.output.routing", "intersect_capabilities"),
}

__all__ = [
    "EndpointRegistration",
    "NoCompatibleOutput",
    "OutputRouter",
    "ProactiveChannelAttempt",
    "ProactiveDeliveryResult",
    "ProactiveDeliveryService",
    "intersect_capabilities",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
