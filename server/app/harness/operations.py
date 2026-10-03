"""Immutable media authority and execution ports, independent of SQL/providers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.schemas import PrivacyLevel

from .unit_costs import UnitCostQuote


@dataclass(frozen=True, slots=True)
class OperationPolicy:
    config_version: int
    budget: tuple[tuple[str, str | int | float | bool | None], ...]
    unit_quote: UnitCostQuote | None = None


@dataclass(frozen=True, slots=True)
class GenerationTicket:
    id: UUID
    user_id: UUID
    privacy_level: PrivacyLevel
    endpoint_fingerprint: str
    source_fingerprint: str


class CapabilityExecution(Protocol):
    async def ticket(self, user_id: UUID, task_id: str) -> GenerationTicket | None: ...

    async def validate_ticket(self, ticket: GenerationTicket) -> None: ...

    async def execute(
        self,
        policy: OperationPolicy,
        *,
        user_id: UUID,
        privacy_level: PrivacyLevel,
        entry: str,
        invoke: Callable[[Callable[[], Awaitable[None]]], Awaitable[object]],
        evidence: Callable[[object], dict[str, str]],
        source_guard: Callable[[], Awaitable[None]],
        cost_endpoint: str,
    ) -> object: ...
