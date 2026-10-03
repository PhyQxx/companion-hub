"""Public config exports; implementations load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .database import DatabaseConfigStore, DatabaseConfigVersion
    from .models import (
        AmapToolConfig,
        BrowserWorkflowConfig,
        CommuteConfig,
        DesktopActionsConfig,
        HomeAssistantConfig,
        HomeAssistantEntityConfig,
        HomeAssistantProactiveRuleConfig,
        HubConfig,
        IntegrationsConfig,
        McpConfig,
        McpServerConfig,
        ObservabilityConfig,
        ProactiveChannelConfig,
        ProactiveOutputConfig,
        QueryToolConfig,
        ToolsConfig,
        WebFetchConfig,
        XiaoAiConfig,
    )
    from .store import ConfigAudit, ConfigSnapshot, ConfigStore, ConfigWatcher

_EXPORTS = {
    "AmapToolConfig": ("app.config.models", "AmapToolConfig"),
    "BrowserWorkflowConfig": ("app.config.models", "BrowserWorkflowConfig"),
    "CommuteConfig": ("app.config.models", "CommuteConfig"),
    "ConfigAudit": ("app.config.store", "ConfigAudit"),
    "ConfigSnapshot": ("app.config.store", "ConfigSnapshot"),
    "ConfigStore": ("app.config.store", "ConfigStore"),
    "ConfigWatcher": ("app.config.store", "ConfigWatcher"),
    "DatabaseConfigStore": ("app.config.database", "DatabaseConfigStore"),
    "DatabaseConfigVersion": ("app.config.database", "DatabaseConfigVersion"),
    "DesktopActionsConfig": ("app.config.models", "DesktopActionsConfig"),
    "HomeAssistantConfig": ("app.config.models", "HomeAssistantConfig"),
    "HomeAssistantEntityConfig": ("app.config.models", "HomeAssistantEntityConfig"),
    "HomeAssistantProactiveRuleConfig": ("app.config.models", "HomeAssistantProactiveRuleConfig"),
    "HubConfig": ("app.config.models", "HubConfig"),
    "IntegrationsConfig": ("app.config.models", "IntegrationsConfig"),
    "McpConfig": ("app.config.models", "McpConfig"),
    "McpServerConfig": ("app.config.models", "McpServerConfig"),
    "ObservabilityConfig": ("app.config.models", "ObservabilityConfig"),
    "ProactiveChannelConfig": ("app.config.models", "ProactiveChannelConfig"),
    "ProactiveOutputConfig": ("app.config.models", "ProactiveOutputConfig"),
    "QueryToolConfig": ("app.config.models", "QueryToolConfig"),
    "ToolsConfig": ("app.config.models", "ToolsConfig"),
    "WebFetchConfig": ("app.config.models", "WebFetchConfig"),
    "XiaoAiConfig": ("app.config.models", "XiaoAiConfig"),
}

__all__ = [
    "AmapToolConfig",
    "BrowserWorkflowConfig",
    "CommuteConfig",
    "ConfigAudit",
    "ConfigSnapshot",
    "ConfigStore",
    "ConfigWatcher",
    "DatabaseConfigStore",
    "DatabaseConfigVersion",
    "DesktopActionsConfig",
    "HomeAssistantConfig",
    "HomeAssistantEntityConfig",
    "HomeAssistantProactiveRuleConfig",
    "HubConfig",
    "IntegrationsConfig",
    "McpConfig",
    "McpServerConfig",
    "ObservabilityConfig",
    "ProactiveChannelConfig",
    "ProactiveOutputConfig",
    "QueryToolConfig",
    "ToolsConfig",
    "WebFetchConfig",
    "XiaoAiConfig",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
