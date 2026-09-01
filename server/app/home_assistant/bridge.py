from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Protocol

from app.config import HomeAssistantConfig, HomeAssistantEntityConfig

from .client import HomeAssistantClient
from .models import (
    HomeAssistantError,
    HomeAssistantHealth,
    HomeAssistantLogEntry,
    HomeAssistantState,
    HomeAssistantStateChange,
    HomeAssistantStatus,
)

logger = logging.getLogger(__name__)


class HomeAssistantGateway(Protocol):
    async def fetch_states(self) -> tuple[HomeAssistantState, ...]: ...

    def state_changes(self) -> AsyncIterator[HomeAssistantStateChange]: ...

    async def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str,
        service_data: dict[str, Any] | None = None,
    ) -> tuple[HomeAssistantState, ...]: ...

    async def fetch_history(
        self, entity_id: str, start: datetime, end: datetime
    ) -> tuple[HomeAssistantState, ...]: ...

    async def fetch_logbook(
        self, entity_id: str, start: datetime, end: datetime
    ) -> tuple[HomeAssistantLogEntry, ...]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _CachedState:
    value: HomeAssistantState
    received_monotonic: float


class HomeAssistantBridge:
    def __init__(
        self,
        config: HomeAssistantConfig,
        gateway: HomeAssistantGateway,
        state_change_handler: Callable[
            [HomeAssistantEntityConfig, HomeAssistantState | None, HomeAssistantState | None],
            Awaitable[None],
        ]
        | None = None,
    ) -> None:
        self.config = config
        self._gateway = gateway
        self._state_change_handler = state_change_handler
        self._policies = {item.entity_id: item for item in config.entities}
        self._cache: dict[str, _CachedState] = {}
        self._status = HomeAssistantStatus.STOPPED
        self._reason_code: str | None = None
        self._last_sync_at: datetime | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._status = HomeAssistantStatus.CONNECTING
        self._task = asyncio.create_task(self._run(), name="aria-home-assistant")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._gateway.close()
        self._status = HomeAssistantStatus.STOPPED

    async def refresh_once(self) -> None:
        states = await self._gateway.fetch_states()
        now = monotonic()
        self._cache = {
            state.entity_id: _CachedState(state, now)
            for state in states
            if self._is_read_allowed(state.entity_id)
        }
        self._last_sync_at = datetime.now(UTC)
        self._status = HomeAssistantStatus.READY
        self._reason_code = None

    def policy(self, entity_id: str) -> HomeAssistantEntityConfig | None:
        policy = self._policies.get(entity_id)
        return policy if policy is not None and policy.read_allowed else None

    def resolve(self, target: str) -> HomeAssistantEntityConfig:
        normalized = target.strip().casefold()
        matches = [
            policy
            for policy in self._policies.values()
            if policy.read_allowed
            and normalized
            in {
                policy.entity_id.casefold(),
                policy.display_name.strip().casefold(),
                *(value.strip().casefold() for value in policy.aliases),
            }
        ]
        if not matches:
            raise HomeAssistantError("ha_entity_not_found")
        if len(matches) > 1:
            raise HomeAssistantError("ha_target_ambiguous")
        return matches[0]

    def get_state(self, entity_id: str) -> HomeAssistantState:
        if not self._is_read_allowed(entity_id):
            raise HomeAssistantError("ha_read_denied")
        cached = self._cache.get(entity_id)
        if cached is None:
            raise HomeAssistantError("ha_entity_unavailable")
        # While the WebSocket subscription is healthy, an unchanged HA state is
        # still authoritative: HA only emits state_changed when something changes.
        # The TTL protects reads after the connection has degraded, not quiet devices.
        if (
            self._status is not HomeAssistantStatus.READY
            and monotonic() - cached.received_monotonic > self.config.state_cache_ttl_seconds
        ):
            raise HomeAssistantError("ha_state_stale")
        return cached.value

    def health(self) -> HomeAssistantHealth:
        return HomeAssistantHealth(
            status=self._status,
            connected=self._status is HomeAssistantStatus.READY,
            cached_entities=len(self._cache),
            last_sync_at=self._last_sync_at,
            reason_code=self._reason_code,
        )

    async def call_service(
        self,
        entity_id: str,
        service: str,
        service_data: dict[str, Any] | None = None,
    ) -> tuple[HomeAssistantState, ...]:
        domain = entity_id.split(".", 1)[0]
        states = await self._gateway.call_service(domain, service, entity_id, service_data)
        now = monotonic()
        for state in states:
            if self._is_read_allowed(state.entity_id):
                self._cache[state.entity_id] = _CachedState(state, now)
        return states

    async def fetch_history(
        self, entity_id: str, start: datetime, end: datetime
    ) -> tuple[HomeAssistantState, ...]:
        return await self._gateway.fetch_history(entity_id, start, end)

    async def fetch_logbook(
        self, entity_id: str, start: datetime, end: datetime
    ) -> tuple[HomeAssistantLogEntry, ...]:
        return await self._gateway.fetch_logbook(entity_id, start, end)

    async def _run(self) -> None:
        backoff = self.config.reconnect_min_seconds
        while not self._stop.is_set():
            try:
                self._status = HomeAssistantStatus.CONNECTING
                await self.refresh_once()
                backoff = self.config.reconnect_min_seconds
                async for change in self._gateway.state_changes():
                    if self._stop.is_set():
                        return
                    if not self._is_read_allowed(change.entity_id):
                        continue
                    old_cached = self._cache.get(change.entity_id)
                    if change.new_state is None:
                        self._cache.pop(change.entity_id, None)
                    else:
                        self._cache[change.entity_id] = _CachedState(
                            change.new_state,
                            monotonic(),
                        )
                    if self._state_change_handler is not None:
                        policy = self._policies[change.entity_id]
                        try:
                            await self._state_change_handler(
                                policy,
                                old_cached.value if old_cached is not None else None,
                                change.new_state,
                            )
                        except Exception:
                            logger.warning(
                                "home assistant state handler failed for %s",
                                change.entity_id,
                                exc_info=True,
                            )
                raise HomeAssistantError("ha_offline")
            except asyncio.CancelledError:
                raise
            except HomeAssistantError as error:
                self._reason_code = error.reason_code
                self._status = (
                    HomeAssistantStatus.AUTH_FAILED
                    if error.reason_code == "ha_auth_failed"
                    else HomeAssistantStatus.DEGRADED
                )
                logger.warning("home assistant bridge degraded: %s", error.reason_code)
            except Exception:
                self._reason_code = "ha_unexpected_error"
                self._status = HomeAssistantStatus.DEGRADED
                logger.warning("home assistant bridge degraded", exc_info=True)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            backoff = min(backoff * 2, self.config.reconnect_max_seconds)

    def _is_read_allowed(self, entity_id: str) -> bool:
        policy = self._policies.get(entity_id)
        return bool(policy is not None and policy.read_allowed)


GatewayFactory = Callable[[HomeAssistantConfig, str], HomeAssistantGateway]


def default_gateway_factory(config: HomeAssistantConfig, token: str) -> HomeAssistantGateway:
    if config.base_url is None:
        raise HomeAssistantError("ha_config_invalid")
    return HomeAssistantClient(
        str(config.base_url).rstrip("/"),
        token,
        verify_tls=config.verify_tls,
        connect_timeout_ms=config.connect_timeout_ms,
        request_timeout_ms=config.request_timeout_ms,
    )
