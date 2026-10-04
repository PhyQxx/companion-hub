"""Authority for an already consumed, session-bound Google authorization."""

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import CalendarOAuthStateRecord, Database
from app.harness.time import utc

from .sync_sources import CalendarSyncSource


def state_identity(row: CalendarOAuthStateRecord) -> tuple[object, ...]:
    return (
        row.id,
        row.user_id,
        row.session_id,
        row.state_hash,
        row.config_version,
        row.config_hash,
        utc(row.created_at),
        utc(row.expires_at),
        utc(row.consumed_at) if row.consumed_at is not None else None,
    )


class GoogleOAuthSource(CalendarSyncSource):
    def __init__(
        self,
        database: Database,
        record: CalendarOAuthStateRecord,
        settings_guard: Callable[[], None],
    ) -> None:
        super().__init__(
            database,
            owner=record.user_id,
            actor_id=record.session_id,
            settings_guard=settings_guard,
        )
        self.authorization_id = record.id
        self.owner = record.user_id
        self.expires_at = utc(record.expires_at)
        self._identity = state_identity(record)

    def _check_memory(self) -> None:
        super()._check_memory()
        if self.expires_at <= datetime.now(UTC):
            raise PermissionError("google_oauth_state_expired")

    async def validate(self, session: AsyncSession, *, lock: bool = False) -> None:
        await super().validate(session, lock=lock)
        query = (
            select(CalendarOAuthStateRecord)
            .where(
                CalendarOAuthStateRecord.id == self.authorization_id,
            )
            .execution_options(populate_existing=True)
        )
        if lock:
            query = query.with_for_update()
        record = await session.scalar(query)
        if (
            record is None
            or record.consumed_at is None
            or record.completed_at is not None
            or state_identity(record) != self._identity
        ):
            raise PermissionError("google_oauth_state_revoked")
        self._check_memory()
