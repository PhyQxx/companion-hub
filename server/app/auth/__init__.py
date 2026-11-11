"""Public auth exports; implementations load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .service import (
        AuthService,
        AuthSession,
        AuthSetupExists,
        ChatPrincipal,
        InvalidCredentials,
        InvalidSession,
        InvalidUsername,
        normalize_username,
    )

_EXPORTS = {
    "AuthService": ("app.auth.service", "AuthService"),
    "AuthSession": ("app.auth.service", "AuthSession"),
    "AuthSetupExists": ("app.auth.service", "AuthSetupExists"),
    "ChatPrincipal": ("app.auth.service", "ChatPrincipal"),
    "InvalidCredentials": ("app.auth.service", "InvalidCredentials"),
    "InvalidSession": ("app.auth.service", "InvalidSession"),
    "InvalidUsername": ("app.auth.service", "InvalidUsername"),
    "normalize_username": ("app.auth.service", "normalize_username"),
}

__all__ = [
    "AuthService",
    "AuthSession",
    "AuthSetupExists",
    "ChatPrincipal",
    "InvalidCredentials",
    "InvalidSession",
    "InvalidUsername",
    "normalize_username",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
