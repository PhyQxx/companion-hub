"""HOME-01 家庭场景：感知事件触发 → 待确认行动计划。Public exports load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .models import (
        MAX_SCENE_STEPS,
        HomeSceneStep,
        HomeSceneTriggered,
        HomeSceneView,
    )
    from .service import HomeSceneService
    from .store import HomeSceneStore
    from .tools import HomeSceneListTool, HomeSceneRunTool

_EXPORTS = {
    "MAX_SCENE_STEPS": ("app.home_scene.models", "MAX_SCENE_STEPS"),
    "HomeSceneListTool": ("app.home_scene.tools", "HomeSceneListTool"),
    "HomeSceneRunTool": ("app.home_scene.tools", "HomeSceneRunTool"),
    "HomeSceneService": ("app.home_scene.service", "HomeSceneService"),
    "HomeSceneStep": ("app.home_scene.models", "HomeSceneStep"),
    "HomeSceneStore": ("app.home_scene.store", "HomeSceneStore"),
    "HomeSceneTriggered": ("app.home_scene.models", "HomeSceneTriggered"),
    "HomeSceneView": ("app.home_scene.models", "HomeSceneView"),
}

__all__ = [
    "MAX_SCENE_STEPS",
    "HomeSceneListTool",
    "HomeSceneRunTool",
    "HomeSceneService",
    "HomeSceneStep",
    "HomeSceneStore",
    "HomeSceneTriggered",
    "HomeSceneView",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
