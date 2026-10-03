"""Revocable account admission for global observation ports."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar
from uuid import UUID

from app.harness.budget import BudgetDenied
from app.harness.guarded_call import guarded_call

T = TypeVar("T")


class ObservationUnavailable(BudgetDenied):
    """An expected source revocation, rather than a provider failure."""


@dataclass(frozen=True, slots=True)
class ObservationOwnerGuard:
    owner: UUID
    resolve: Callable[[], Awaitable[UUID | None]]
    source_active: Callable[[], bool] | None = None
    source_valid: Callable[[], Awaitable[bool]] | None = None

    def enabled(self) -> bool:
        return self.source_active is None or self.source_active()

    async def valid(self) -> bool:
        try:
            await self.check()
        except ObservationUnavailable:
            return False
        return True

    async def check(self) -> None:
        if not self.enabled():
            raise ObservationUnavailable("observation_source_inactive")
        if await self.resolve() != self.owner:
            raise ObservationUnavailable("observation_owner_changed")
        if not self.enabled():
            raise ObservationUnavailable("observation_source_inactive")
        if self.source_valid is not None:
            if not await self.source_valid():
                raise ObservationUnavailable("observation_device_changed")
            # Device resolution can await I/O; recheck account and config after it.
            if await self.resolve() != self.owner:
                raise ObservationUnavailable("observation_owner_changed")
            if not self.enabled():
                raise ObservationUnavailable("observation_source_inactive")

    async def call(self, invoke: Callable[[], Awaitable[T]]) -> T:
        # Validate before starting, while waiting, and before accepting a result.
        # Cancelling a cooperative port cannot undo an external request already sent.
        return await guarded_call(invoke, self.check)
