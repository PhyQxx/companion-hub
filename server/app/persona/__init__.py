"""Public persona exports; implementations load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from app.schemas.reply import Emotion

    from .models import PersonaConfig
    from .store import PersonaSnapshot, PersonaStore, PersonaVersion, hash_persona

_EXPORTS = {
    "Emotion": ("app.schemas.reply", "Emotion"),
    "PersonaConfig": ("app.persona.models", "PersonaConfig"),
    "PersonaSnapshot": ("app.persona.store", "PersonaSnapshot"),
    "PersonaStore": ("app.persona.store", "PersonaStore"),
    "PersonaVersion": ("app.persona.store", "PersonaVersion"),
    "hash_persona": ("app.persona.store", "hash_persona"),
}

__all__ = [
    "Emotion",
    "PersonaConfig",
    "PersonaSnapshot",
    "PersonaStore",
    "PersonaVersion",
    "hash_persona",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
