"""Revocable account admission for global observation ports."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar
from uuid import UUID

from app.harness.budget import BudgetDenied
from app.harness.guarded_call import guarded_call

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ObservationOwnerGuard:
    owner: UUID
    resolve: Callable[[], Awaitable[UUID | None]]

    async def valid(self) -> bool:
        return await self.resolve() == self.owner

    async def check(self) -> None:
        if not await self.valid():
            raise BudgetDenied("observation_owner_changed")

    async def call(self, invoke: Callable[[], Awaitable[T]]) -> T:
        # Validate before starting, while waiting, and before accepting a result.
        # Cancelling a cooperative port cannot undo an external request already sent.
        return await guarded_call(invoke, self.check)
