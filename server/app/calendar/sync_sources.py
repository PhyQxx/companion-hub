"""Live calendar connection authority; credentials never leave this scope."""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TypeVar
from uuid import UUID

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AppUserRecord, AuthSessionRecord, CalendarOAuthTokenRecord, Database
from app.harness.guarded_call import guarded_inline_call
from app.harness.time import utc

T = TypeVar("T")


class CalendarSyncSource:
    def __init__(
        self,
        database: Database,
        *,
        owner: UUID,
        settings_guard: Callable[[], None],
        token: CalendarOAuthTokenRecord | None = None,
        actor_id: UUID | None = None,
    ) -> None:
        self._database, self._owner, self._settings_guard = database, owner, settings_guard
        self.closing = False
        self._actor_id = actor_id
        self._actor_expires_at: datetime | None = None
        self._token = (
            (token.id, token.refresh_token, token.account_email, token.obtained_at)
            if token is not None
            else None
        )

    async def validate(self, session: AsyncSession, *, lock: bool = False) -> None:
        self._check_memory()
        owner = await session.scalar(
            select(AppUserRecord.id).where(
                AppUserRecord.id == self._owner,
                AppUserRecord.status == "active",
            )
        )
        if owner is None:
            raise PermissionError("calendar_sync_owner_inactive")
        if self._actor_id is not None:
            actor_query = (
                select(AuthSessionRecord)
                .where(
                    AuthSessionRecord.id == self._actor_id,
                    AuthSessionRecord.user_id == self._owner,
                    AuthSessionRecord.revoked_at.is_(None),
                    AuthSessionRecord.expires_at > datetime.now(UTC),
                )
                .execution_options(populate_existing=True)
            )
            if lock:
                actor_query = actor_query.with_for_update()
            actor = await session.scalar(actor_query)
            if actor is None:
                raise PermissionError("calendar_sync_actor_invalid")
            expires = utc(actor.expires_at)
            self._actor_expires_at = (
                min(self._actor_expires_at, expires) if self._actor_expires_at else expires
            )
        if self._token is not None:
            query = (
                select(CalendarOAuthTokenRecord)
                .where(
                    CalendarOAuthTokenRecord.id == self._token[0],
                    CalendarOAuthTokenRecord.user_id == self._owner,
                    CalendarOAuthTokenRecord.provider == "google",
                )
                .execution_options(populate_existing=True)
            )
            if lock:
                query = query.with_for_update()
            token = await session.scalar(query)
            if (
                token is None
                or (token.id, token.refresh_token, token.account_email, token.obtained_at)
                != self._token
            ):
                raise PermissionError("calendar_sync_source_changed")
        self._check_memory()

    def _check_memory(self) -> None:
        self._settings_guard()
        if self._actor_expires_at is not None and self._actor_expires_at <= datetime.now(UTC):
            raise PermissionError("calendar_sync_actor_invalid")

    async def check_memory(self) -> None:
        self._check_memory()

    async def check(self) -> None:
        async with self._database.sessions() as session:
            await self.validate(session)

    async def call(self, invoke: Callable[[], Awaitable[T]]) -> T:
        await self.check()
        result = await guarded_inline_call(invoke, self.check)
        await self.check()
        return result

    @contextmanager
    def commit_fence(self, session: AsyncSession) -> Iterator[None]:
        def check(*args: object) -> None:
            self._check_memory()

        events = ("before_flush", "after_flush_postexec", "before_commit")
        for name in events:
            event.listen(session.sync_session, name, check)
        try:
            yield
        finally:
            for name in events:
                event.remove(session.sync_session, name, check)
