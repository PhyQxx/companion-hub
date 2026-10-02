"""Immutable source views and repository port; no domain ORM rows cross it."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.harness.time import utc


@dataclass(frozen=True)
class GoalDeliveryClaim:
    phase: str
    claimed_at: datetime
    timezone: str


@dataclass(frozen=True)
class DeliverySourceSnapshot:
    privacy_level: str
    generation: int | None = None
    goal_claim: GoalDeliveryClaim | None = None

    def contract_fields(self) -> dict[str, object]:
        fields: dict[str, object] = {}
        if self.generation is not None:
            fields["source_generation"] = self.generation
        if self.goal_claim is not None:
            fields.update(
                source_claimed_at=utc(self.goal_claim.claimed_at).isoformat(),
                source_phase=self.goal_claim.phase,
                source_timezone=self.goal_claim.timezone,
            )
        return fields


class DeliverySourceRepository(Protocol):
    @property
    def source_id(self) -> UUID: ...
    @property
    def user_id(self) -> UUID: ...
    def request_key(self, entry: str, run_id: UUID) -> str: ...
    async def lock(self, session: AsyncSession, *, pending: bool = False) -> bool: ...
    async def inspect(
        self, session: AsyncSession, fingerprint: str, run_id: UUID
    ) -> DeliverySourceSnapshot: ...
    async def finish(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        entry: str,
        channels: list[str],
        reason: str | None,
        state: str,
        now: datetime,
    ) -> bool: ...
