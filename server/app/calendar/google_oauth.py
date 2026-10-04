"""Durable one-time Google authorization bound to the initiating session."""

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select, update

from app.config import ConfigStore, DatabaseConfigStore
from app.config.models import GoogleCalendarConfig
from app.db import AppUserRecord, AuthSessionRecord, CalendarOAuthStateRecord, Database
from app.harness.time import utc
from app.ids import uuid7
from app.runs.google_oauth import exchange_google_authorization

from .google import STATE_TTL_SECONDS, GoogleTokenStore, build_authorize_url, resolve_google_secret
from .oauth_sources import GoogleOAuthSource
from .sync_sources import CalendarSyncSource


class GoogleOAuthStateInvalid(ValueError):
    def __init__(self) -> None:
        super().__init__("google_oauth_state_invalid")


def connection_hash(version: int, config: GoogleCalendarConfig, secret: str, key: str) -> str:
    payload = json.dumps(
        {"version": version, "config": config.model_dump(mode="json"), "resolved_secret": secret},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hmac.new(key.encode(), payload, hashlib.sha256).hexdigest()


class GoogleOAuthService:
    def __init__(
        self,
        database: Database,
        store: ConfigStore | DatabaseConfigStore,
        state_key: Callable[[], str | None],
    ) -> None:
        self._database, self._store, self._state_key = database, store, state_key

    def _connection(self) -> tuple[int, GoogleCalendarConfig, str, str]:
        snapshot = self._store.current
        config = deepcopy(snapshot.config.integrations.calendar.google)
        secret, key = resolve_google_secret(config), self._state_key()
        if not key:
            raise PermissionError("google_oauth_key_unavailable")
        if not config.enabled or not config.client_id or not config.redirect_uri or not secret:
            raise PermissionError("google_calendar_not_configured")
        return snapshot.version, config, secret, key

    def _settings_guard(
        self, version: int, fingerprint: str, expires: datetime
    ) -> Callable[[], None]:
        def check() -> None:
            current_version, config, secret, key = self._connection()
            if current_version != version or not hmac.compare_digest(
                connection_hash(current_version, config, secret, key),
                fingerprint,
            ):
                raise PermissionError("calendar_sync_source_changed")
            if utc(expires) <= datetime.now(UTC):
                raise PermissionError("google_oauth_state_expired")

        return check

    async def status(self, user_id: UUID) -> dict[str, bool]:
        try:
            self._connection()
            configured = True
        except PermissionError:
            configured = False
        return {
            "configured": configured,
            "connected": await GoogleTokenStore(self._database).get(user_id) is not None,
        }

    async def authorize(self, user_id: UUID, session_id: UUID) -> str:
        version, config, secret, key = self._connection()
        now = datetime.now(UTC)
        state = secrets.token_hex(32)
        fingerprint = connection_hash(version, config, secret, key)
        expires = now + timedelta(seconds=STATE_TTL_SECONDS)
        source = CalendarSyncSource(
            self._database,
            owner=user_id,
            actor_id=session_id,
            settings_guard=self._settings_guard(version, fingerprint, expires),
        )
        await source.check()
        async with self._database.sessions() as sql:
            with source.commit_fence(sql):
                async with sql.begin():
                    owner = await sql.scalar(
                        update(AppUserRecord)
                        .where(
                            AppUserRecord.id == user_id,
                            AppUserRecord.status == "active",
                        )
                        .values(id=AppUserRecord.id)
                        .returning(AppUserRecord.id)
                    )
                    if owner is None:
                        raise PermissionError("calendar_sync_owner_inactive")
                    await source.validate(sql, lock=True)
                    actor = await sql.get_one(AuthSessionRecord, session_id)
                    expires = min(expires, utc(actor.expires_at))
                    # The latest authorization replaces older pending or in-flight
                    # requests under the same owner lock used by token installation.
                    await sql.execute(
                        delete(CalendarOAuthStateRecord).where(
                            CalendarOAuthStateRecord.user_id == user_id,
                        )
                    )
                    sql.add(
                        CalendarOAuthStateRecord(
                            id=uuid7(),
                            user_id=user_id,
                            session_id=session_id,
                            state_hash=hashlib.sha256(state.encode()).hexdigest(),
                            config_version=version,
                            config_hash=fingerprint,
                            created_at=now,
                            expires_at=expires,
                        )
                    )
                    await sql.flush()
                    await source.validate(sql, lock=True)
        assert config.client_id is not None and config.redirect_uri is not None
        return build_authorize_url(
            client_id=config.client_id, redirect_uri=config.redirect_uri, state=state
        )

    async def _consume(self, state: str) -> tuple[GoogleOAuthSource, GoogleCalendarConfig, str]:
        if re.fullmatch(r"[0-9a-f]{64}", state) is None:
            raise GoogleOAuthStateInvalid()
        digest = hashlib.sha256(state.encode()).hexdigest()
        async with self._database.sessions() as sql:
            record = await sql.scalar(
                select(CalendarOAuthStateRecord).where(
                    CalendarOAuthStateRecord.state_hash == digest,
                )
            )
        if (
            record is None
            or record.consumed_at is not None
            or utc(record.expires_at) <= datetime.now(UTC)
        ):
            raise GoogleOAuthStateInvalid()
        check_settings = self._settings_guard(
            record.config_version, record.config_hash, record.expires_at
        )
        source = CalendarSyncSource(
            self._database,
            owner=record.user_id,
            actor_id=record.session_id,
            settings_guard=check_settings,
        )
        await source.check()
        async with self._database.sessions() as sql:
            with source.commit_fence(sql):
                async with sql.begin():
                    owner = await sql.scalar(
                        update(AppUserRecord)
                        .where(
                            AppUserRecord.id == record.user_id,
                            AppUserRecord.status == "active",
                        )
                        .values(id=AppUserRecord.id)
                        .returning(AppUserRecord.id)
                    )
                    if owner is None:
                        raise PermissionError("calendar_sync_owner_inactive")
                    await source.validate(sql, lock=True)
                    accepted = await sql.scalar(
                        update(CalendarOAuthStateRecord)
                        .where(
                            CalendarOAuthStateRecord.id == record.id,
                            CalendarOAuthStateRecord.user_id == record.user_id,
                            CalendarOAuthStateRecord.session_id == record.session_id,
                            CalendarOAuthStateRecord.state_hash == digest,
                            CalendarOAuthStateRecord.config_version == record.config_version,
                            CalendarOAuthStateRecord.config_hash == record.config_hash,
                            CalendarOAuthStateRecord.consumed_at.is_(None),
                            CalendarOAuthStateRecord.completed_at.is_(None),
                            CalendarOAuthStateRecord.expires_at > datetime.now(UTC),
                        )
                        .values(consumed_at=datetime.now(UTC))
                        .returning(CalendarOAuthStateRecord)
                        .execution_options(synchronize_session=False, populate_existing=True)
                    )
                    if accepted is None:
                        raise GoogleOAuthStateInvalid()
                    await sql.flush()
                    await source.validate(sql, lock=True)
        _, config, secret, _ = self._connection()
        return GoogleOAuthSource(self._database, accepted, check_settings), config, secret

    async def callback(self, *, state: str, code: str, error: str) -> dict[str, str]:
        if bool(code) == bool(error) or (code and not code.strip()):
            raise GoogleOAuthStateInvalid()
        source, config, secret = await self._consume(state)
        if error:
            return {"status": "denied", "detail": "google_authorization_denied"}
        _, email = await exchange_google_authorization(
            self._database,
            self._store,
            source=source,
            config=config,
            secret=secret,
            code=code,
        )
        return {"status": "ok", "account": email or ""}
