"""Public avatar exports; implementations load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .control import AvatarControlPublisher, control_from_agent_reply, with_reply_text
    from .importer import AvatarAssetImporter, AvatarImportResult
    from .store import AvatarBindingView, AvatarInstanceView, AvatarPackView, AvatarStore

_EXPORTS = {
    "AvatarAssetImporter": ("app.avatar.importer", "AvatarAssetImporter"),
    "AvatarBindingView": ("app.avatar.store", "AvatarBindingView"),
    "AvatarControlPublisher": ("app.avatar.control", "AvatarControlPublisher"),
    "AvatarImportResult": ("app.avatar.importer", "AvatarImportResult"),
    "AvatarInstanceView": ("app.avatar.store", "AvatarInstanceView"),
    "AvatarPackView": ("app.avatar.store", "AvatarPackView"),
    "AvatarStore": ("app.avatar.store", "AvatarStore"),
    "control_from_agent_reply": ("app.avatar.control", "control_from_agent_reply"),
    "with_reply_text": ("app.avatar.control", "with_reply_text"),
}

__all__ = [
    "AvatarAssetImporter",
    "AvatarBindingView",
    "AvatarControlPublisher",
    "AvatarImportResult",
    "AvatarInstanceView",
    "AvatarPackView",
    "AvatarStore",
    "control_from_agent_reply",
    "with_reply_text",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
