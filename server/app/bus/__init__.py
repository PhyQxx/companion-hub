"""Public bus exports; implementations load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .dispatcher import DispatchResult, EventPublisher, dispatch_once
    from .router import LocalEventPublisher
    from .service import EventHandler, append_event, consume_event
    from .worker import DispatcherWorker, WorkerState

_EXPORTS = {
    "DispatchResult": ("app.bus.dispatcher", "DispatchResult"),
    "DispatcherWorker": ("app.bus.worker", "DispatcherWorker"),
    "EventHandler": ("app.bus.service", "EventHandler"),
    "EventPublisher": ("app.bus.dispatcher", "EventPublisher"),
    "LocalEventPublisher": ("app.bus.router", "LocalEventPublisher"),
    "WorkerState": ("app.bus.worker", "WorkerState"),
    "append_event": ("app.bus.service", "append_event"),
    "consume_event": ("app.bus.service", "consume_event"),
    "dispatch_once": ("app.bus.dispatcher", "dispatch_once"),
}

__all__ = [
    "DispatchResult",
    "DispatcherWorker",
    "EventHandler",
    "EventPublisher",
    "LocalEventPublisher",
    "WorkerState",
    "append_event",
    "consume_event",
    "dispatch_once",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
