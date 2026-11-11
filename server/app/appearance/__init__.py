"""Public appearance exports; implementations load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .store import (
        DEFAULT_DARK_TIME,
        DEFAULT_LIGHT_TIME,
        ThemePreferenceView,
        ThemeStore,
        ThemeView,
        schedule_prefers_light,
        validate_schedule,
    )

_EXPORTS = {
    "DEFAULT_DARK_TIME": ("app.appearance.store", "DEFAULT_DARK_TIME"),
    "DEFAULT_LIGHT_TIME": ("app.appearance.store", "DEFAULT_LIGHT_TIME"),
    "ThemePreferenceView": ("app.appearance.store", "ThemePreferenceView"),
    "ThemeStore": ("app.appearance.store", "ThemeStore"),
    "ThemeView": ("app.appearance.store", "ThemeView"),
    "schedule_prefers_light": ("app.appearance.store", "schedule_prefers_light"),
    "validate_schedule": ("app.appearance.store", "validate_schedule"),
}

__all__ = [
    "DEFAULT_DARK_TIME",
    "DEFAULT_LIGHT_TIME",
    "ThemePreferenceView",
    "ThemeStore",
    "ThemeView",
    "schedule_prefers_light",
    "validate_schedule",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
