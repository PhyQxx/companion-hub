"""A revocable in-memory focus source backed by a durable per-cycle Run identity."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncSession

from app.context.repository import ContextSourceInvalidated, validate_references
from app.harness.budget import BudgetDenied
from app.harness.context import ContextReference
from app.harness.time import utc
from app.runs.delivery_contracts import DeliverySourceSnapshot

from .analysis import FocusSession, FocusSignal
from .service import FocusService


@dataclass(frozen=True)
class FocusDeliverySource:
    source_id: UUID
    user_id: UUID
    service: FocusService
    session: FocusSession
    signal: FocusSignal
    references: tuple[ContextReference, ...]
    claimed_at: datetime
    cycle: int

    @property
    def run_id(self) -> UUID:
        return uuid5(self.source_id, f"{self.signal.kind}:{self.cycle}")

    def request_key(self, entry: str, run_id: UUID) -> str:
        return f"{entry}:{run_id}"

    def _current(self) -> FocusSession | None:
        current = self.service.get_session(str(self.user_id))
        if current is None or current.session_id != self.session.session_id:
            return None
        if (current.target, current.target_keywords, current.started_at, current.ends_at) != (
            self.session.target,
            self.session.target_keywords,
            self.session.started_at,
            self.session.ends_at,
        ):
            return None
        if current.nudged_at.get(self.signal.kind) != self.session.nudged_at.get(self.signal.kind):
            return None
        return current

    async def lock(self, session: AsyncSession, *, pending: bool = False) -> bool:
        # The unique Run ID serializes this ephemeral cycle in the database.
        return self._current() is not None

    async def inspect(
        self, session: AsyncSession, fingerprint: str, run_id: UUID
    ) -> DeliverySourceSnapshot:
        if (
            self._current() is None
            or run_id != self.run_id
            or fingerprint != hashlib.sha256(self.signal.message.encode()).hexdigest()
        ):
            raise BudgetDenied("delivery_source_changed")
        if not self.references or len(self.references) > 200:
            raise BudgetDenied("delivery_source_invalid")
        try:
            await validate_references(
                session, self.references, owner_id=self.user_id, privacy_level="L1"
            )
        except ContextSourceInvalidated as error:
            raise BudgetDenied("delivery_source_changed") from error
        return DeliverySourceSnapshot(privacy_level="L1", generation=self.cycle)

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
    ) -> bool:
        if self._current() is None:
            return True
        if run_id == self.run_id:
            # Consume unknown attempts too; never retry this cycle after a timeout.
            self.service.mark_nudged(str(self.user_id), self.signal, now=utc(self.claimed_at))
        return False
