"""Public skills exports; implementations load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .importer import import_skill_zip, parse_skill_markdown
    from .models import (
        SkillApiManifest,
        SkillDocument,
        SkillLoginAuth,
        SkillOperation,
        SkillParameter,
    )

_EXPORTS = {
    "SkillApiManifest": ("app.skills.models", "SkillApiManifest"),
    "SkillDocument": ("app.skills.models", "SkillDocument"),
    "SkillLoginAuth": ("app.skills.models", "SkillLoginAuth"),
    "SkillOperation": ("app.skills.models", "SkillOperation"),
    "SkillParameter": ("app.skills.models", "SkillParameter"),
    "import_skill_zip": ("app.skills.importer", "import_skill_zip"),
    "parse_skill_markdown": ("app.skills.importer", "parse_skill_markdown"),
}

__all__ = [
    "SkillApiManifest",
    "SkillDocument",
    "SkillLoginAuth",
    "SkillOperation",
    "SkillParameter",
    "import_skill_zip",
    "parse_skill_markdown",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
