"""Immutable media authority and execution ports, independent of SQL/providers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
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
class OperationCost:
    call_id: UUID
    user_id: UUID
    endpoint: str
    quote: UnitCostQuote


_OPERATION_COST: ContextVar[OperationCost | None] = ContextVar("operation_cost", default=None)


def current_operation_cost() -> OperationCost | None:
    return _OPERATION_COST.get()


@contextmanager
def operation_cost_scope(cost: OperationCost | None) -> Iterator[None]:
    token = _OPERATION_COST.set(cost)
    try:
        yield
    finally:
        _OPERATION_COST.reset(token)


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
