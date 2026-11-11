"""Public jobs exports; implementations load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .asset_store import AssetStore, AssetView
    from .delegated import (
        DELEG_KIND_RESEARCH,
        DELEG_RESOURCE_CLASS,
        DelegatedJobWorker,
        DelegateTaskTool,
        DelegCancelled,
        DelegRunContext,
        WebResearchHandler,
        cancel_turn_delegations,
    )
    from .engine import JobEngine, JobStatus, JobView

_EXPORTS = {
    "DELEG_KIND_RESEARCH": ("app.jobs.delegated", "DELEG_KIND_RESEARCH"),
    "DELEG_RESOURCE_CLASS": ("app.jobs.delegated", "DELEG_RESOURCE_CLASS"),
    "AssetStore": ("app.jobs.asset_store", "AssetStore"),
    "AssetView": ("app.jobs.asset_store", "AssetView"),
    "DelegCancelled": ("app.jobs.delegated", "DelegCancelled"),
    "DelegRunContext": ("app.jobs.delegated", "DelegRunContext"),
    "DelegateTaskTool": ("app.jobs.delegated", "DelegateTaskTool"),
    "DelegatedJobWorker": ("app.jobs.delegated", "DelegatedJobWorker"),
    "JobEngine": ("app.jobs.engine", "JobEngine"),
    "JobStatus": ("app.jobs.engine", "JobStatus"),
    "JobView": ("app.jobs.engine", "JobView"),
    "WebResearchHandler": ("app.jobs.delegated", "WebResearchHandler"),
    "cancel_turn_delegations": ("app.jobs.delegated", "cancel_turn_delegations"),
}

__all__ = [
    "DELEG_KIND_RESEARCH",
    "DELEG_RESOURCE_CLASS",
    "AssetStore",
    "AssetView",
    "DelegCancelled",
    "DelegRunContext",
    "DelegateTaskTool",
    "DelegatedJobWorker",
    "JobEngine",
    "JobStatus",
    "JobView",
    "WebResearchHandler",
    "cancel_turn_delegations",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
