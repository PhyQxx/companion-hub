from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from app.chat.capabilities import RuntimeActionCapability
from app.config import ConfigStore, DatabaseConfigStore, HomeAssistantEntityConfig
from app.llm.provider import EnvSecretProvider, SecretNotFound

from .bridge import GatewayFactory, HomeAssistantBridge, default_gateway_factory
from .models import (
    HomeAssistantError,
    HomeAssistantHealth,
    HomeAssistantLogEntry,
    HomeAssistantState,
    HomeAssistantStatus,
)

StateChangeHandler = Callable[
    [HomeAssistantEntityConfig, HomeAssistantState | None, HomeAssistantState | None],
    Awaitable[None],
]


def home_assistant_service_for_action(action: str) -> str:
    """Translate the bounded semantic action catalog to a Home Assistant service."""
    return {
        "set_brightness": "turn_on",
        "play": "media_play",
        "pause": "media_pause",
    }.get(action, action)


class HomeAssistantManager:
    """Lazy runtime wrapper so HA failure never prevents Hub startup."""

    def __init__(
        self,
        config_store: ConfigStore | DatabaseConfigStore,
        *,
        secrets: EnvSecretProvider | None = None,
        gateway_factory: GatewayFactory = default_gateway_factory,
    ) -> None:
        self._config_store = config_store
        self._secrets = secrets or EnvSecretProvider()
        self._gateway_factory = gateway_factory
        self._bridge: HomeAssistantBridge | None = None
        self._startup_reason: str | None = None
        self._state_change_handler: StateChangeHandler | None = None

    def set_state_change_handler(self, handler: StateChangeHandler | None) -> None:
        self._state_change_handler = handler

    async def start(self) -> None:
        self._startup_reason = None
        config = self._config_store.current.config.integrations.home_assistant
        if not config.enabled:
            return
        if config.secret_ref is None and config.secret_value is None:
            self._startup_reason = "ha_config_invalid"
            return
        try:
            token = (
                config.secret_value
                if config.secret_value is not None
                else self._secrets.resolve(config.secret_ref or "")
            )
            gateway = self._gateway_factory(config, token)
        except SecretNotFound:
            self._startup_reason = "ha_secret_unavailable"
            return
        except HomeAssistantError as error:
            self._startup_reason = error.reason_code
            return
        self._bridge = HomeAssistantBridge(
            config, gateway, state_change_handler=self._state_change_handler
        )
        await self._bridge.start()

    async def reconfigure(self) -> None:
        """Apply the currently published HA configuration without restarting Hub."""
        await self.stop()
        self._bridge = None
        await self.start()

    async def stop(self) -> None:
        if self._bridge is not None:
            await self._bridge.stop()

    def health(self) -> HomeAssistantHealth:
        config = self._config_store.current.config.integrations.home_assistant
        if not config.enabled:
            return HomeAssistantHealth(
                status=HomeAssistantStatus.DISABLED,
                connected=False,
                cached_entities=0,
            )
        if self._bridge is None:
            return HomeAssistantHealth(
                status=HomeAssistantStatus.AUTH_FAILED,
                connected=False,
                cached_entities=0,
                reason_code=self._startup_reason or "ha_not_started",
            )
        return self._bridge.health()

    async def available_actions(self, user_id: UUID) -> Sequence[RuntimeActionCapability]:
        del user_id
        if self._bridge is None or not self.health().connected:
            return ()
        actions: list[RuntimeActionCapability] = []
        for policy in self._bridge.config.entities:
            if not policy.read_allowed or policy.privacy_level == "L3":
                continue
            try:
                self._bridge.get_state(policy.entity_id)
            except HomeAssistantError:
                continue
            actions.append(
                RuntimeActionCapability(
                    capability_id=(f"home_assistant:{policy.entity_id}:state.read"),
                    label=policy.display_name,
                    description="可以读取这个已授权 Home Assistant 实体的当前状态",
                )
            )
            if policy.history_allowed:
                actions.append(
                    RuntimeActionCapability(
                        capability_id=(f"home_assistant:{policy.entity_id}:history.read"),
                        label=f"{policy.display_name}历史",
                        description="可以读取这个实体已授权时间范围内的状态历史和日志",
                    )
                )
            for action in policy.allowed_actions:
                confirmation = (
                    ", 需要用户明确确认" if action in policy.confirmation_required_actions else ""
                )
                actions.append(
                    RuntimeActionCapability(
                        capability_id=f"home_assistant:{policy.entity_id}:{action}",
                        label=f"{policy.display_name} · {action}",
                        description=f"可以执行已授权的 Home Assistant 控制{confirmation}",
                    )
                )
        return tuple(actions)

    def resolve(self, target: str) -> HomeAssistantEntityConfig:
        if self._bridge is None:
            raise HomeAssistantError("ha_offline")
        return self._bridge.resolve(target)

    def get_state(self, entity_id: str) -> HomeAssistantState:
        if self._bridge is None:
            raise HomeAssistantError("ha_offline")
        return self._bridge.get_state(entity_id)

    async def control(
        self,
        target: str,
        action: str,
        *,
        service_data: dict[str, Any] | None = None,
    ) -> HomeAssistantEntityConfig:
        if self._bridge is None:
            raise HomeAssistantError("ha_offline")
        policy = self._bridge.resolve(target)
        if action not in policy.allowed_actions:
            raise HomeAssistantError("ha_action_denied")
        service = home_assistant_service_for_action(action)
        await self._bridge.call_service(policy.entity_id, service, service_data)
        return policy

    async def history(
        self,
        target: str,
        start: datetime,
        end: datetime,
    ) -> tuple[HomeAssistantEntityConfig, tuple[HomeAssistantState, ...]]:
        if self._bridge is None:
            raise HomeAssistantError("ha_offline")
        policy = self._bridge.resolve(target)
        if not policy.history_allowed:
            raise HomeAssistantError("ha_history_denied")
        rows = await self._bridge.fetch_history(policy.entity_id, start, end)
        return policy, rows

    async def logbook(
        self,
        target: str,
        start: datetime,
        end: datetime,
    ) -> tuple[HomeAssistantEntityConfig, tuple[HomeAssistantLogEntry, ...]]:
        if self._bridge is None:
            raise HomeAssistantError("ha_offline")
        policy = self._bridge.resolve(target)
        if not policy.history_allowed:
            raise HomeAssistantError("ha_history_denied")
        rows = await self._bridge.fetch_logbook(policy.entity_id, start, end)
        return policy, rows
