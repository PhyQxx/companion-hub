"""CONTACT-01 public exports resolved without loading SQL or tool adapters."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .models import ContactImportantDate, ContactPreference, ContactView
    from .store import ContactStore
    from .tools import ContactQueryTool, ContactSaveTool

_EXPORTS = {
    "ContactImportantDate": ("app.contacts.models", "ContactImportantDate"),
    "ContactPreference": ("app.contacts.models", "ContactPreference"),
    "ContactQueryTool": ("app.contacts.tools", "ContactQueryTool"),
    "ContactSaveTool": ("app.contacts.tools", "ContactSaveTool"),
    "ContactStore": ("app.contacts.store", "ContactStore"),
    "ContactView": ("app.contacts.models", "ContactView"),
}


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "ContactImportantDate",
    "ContactPreference",
    "ContactQueryTool",
    "ContactSaveTool",
    "ContactStore",
    "ContactView",
]
