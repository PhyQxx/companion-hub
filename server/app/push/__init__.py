"""Public push exports; implementations load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .adapter import WebPushAdapter
    from .sender import PushSendResult, PushSendStatus, VapidCredentials, WebPushSender
    from .store import PushSubscriptionStore

_EXPORTS = {
    "PushSendResult": ("app.push.sender", "PushSendResult"),
    "PushSendStatus": ("app.push.sender", "PushSendStatus"),
    "PushSubscriptionStore": ("app.push.store", "PushSubscriptionStore"),
    "VapidCredentials": ("app.push.sender", "VapidCredentials"),
    "WebPushAdapter": ("app.push.adapter", "WebPushAdapter"),
    "WebPushSender": ("app.push.sender", "WebPushSender"),
}

__all__ = [
    "PushSendResult",
    "PushSendStatus",
    "PushSubscriptionStore",
    "VapidCredentials",
    "WebPushAdapter",
    "WebPushSender",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
