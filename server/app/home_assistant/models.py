from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class HomeAssistantError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class HomeAssistantStatus(StrEnum):
    DISABLED = "disabled"
    CONNECTING = "connecting"
    READY = "ready"
    DEGRADED = "degraded"
    AUTH_FAILED = "auth_failed"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class HomeAssistantState:
    entity_id: str
    state: str
    attributes: dict[str, Any]
    last_changed: datetime | None
    last_updated: datetime | None


@dataclass(frozen=True, slots=True)
class HomeAssistantStateChange:
    entity_id: str
    new_state: HomeAssistantState | None


@dataclass(frozen=True, slots=True)
class HomeAssistantLogEntry:
    entity_id: str
    when: datetime | None
    name: str | None
    message: str | None
    domain: str | None


@dataclass(frozen=True, slots=True)
class HomeAssistantHealth:
    status: HomeAssistantStatus
    connected: bool
    cached_entities: int
    last_sync_at: datetime | None = None
    reason_code: str | None = None
