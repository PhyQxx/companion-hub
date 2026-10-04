"""CAL-01 Google 日历：OAuth 刷新令牌流 + Calendar API v3 只读镜像。

流程：
1. Admin/用户在 Google Cloud Console 建 OAuth 客户端（Desktop/Web 均可），
   把 client_id/secret 配进 `integrations.calendar.google`，redirect_uri
   填 Hub 的 `/api/v1/calendar/google/callback`；
2. 用户携带真实聊天会话调用 `GET /api/v1/calendar/google/authorize`，生成
   持久化单次 state 并返回
   授权 URL，用户在浏览器完成同意；
3. callback 消费 state、复核发起会话与配置，通过 owned Run 用 code
   换 refresh token，与费用和 Run 终态同事务落库
   （`calendar_oauth_token`，按用户+google 唯一）；
4. `GoogleCalendarSyncService` 用刷新令牌拉取时间窗事件
   （`singleEvents=true` 由服务端展开周期），经共享镜像引擎 upsert。

安全：刷新令牌只落库不进日志/配置文档；随机 state 只存摘要，最多有效
10 分钟且绑定发起会话，后续授权或断开会撤销旧请求。旧签名帮助函数仅
保留兼容用途，API 回调不接受旧签名 state。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.calendar.mirror import (
    CalendarMirrorService,
    MirrorOccurrence,
    merge_calendar_occurrences,
)
from app.config import ConfigStore, DatabaseConfigStore
from app.config.models import GoogleCalendarConfig
from app.db import AppUserRecord, CalendarOAuthStateRecord, CalendarOAuthTokenRecord, Database
from app.harness.joined_read import join_on_cancel
from app.runs.calendar_sync import CalendarSyncBatch, owned_calendar_sync

from .sync_requests import CalendarRequestRunner
from .sync_sources import CalendarSyncSource

logger = logging.getLogger("app.calendar.google")

SOURCE_GOOGLE = "google"
DEFAULT_TZ = ZoneInfo("Asia/Shanghai")
STATE_TTL_SECONDS = 600
MAX_EVENT_PAGES = 100
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
_SCOPES = "https://www.googleapis.com/auth/calendar.readonly"


class GoogleCalendarError(RuntimeError):
    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        super().__init__(detail or reason_code)


@dataclass(slots=True)
class SyncStats:
    calendars: int = 0
    pulled: int = 0
    mirrors_created: int = 0
    mirrors_updated: int = 0
    mirrors_cancelled: int = 0
    errors: list[str] = field(default_factory=list)


def resolve_google_secret(config: GoogleCalendarConfig) -> str | None:
    import os

    if config.secret_value is not None:
        return config.secret_value
    if config.secret_ref is not None:
        return os.environ.get(config.secret_ref.removeprefix("env:"))
    return None


def _to_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=DEFAULT_TZ).astimezone(UTC)
    return parsed.astimezone(UTC)


def _anchor_stamp(value: str) -> str:
    """原始 ISO 时间的紧凑形式（不换算时区），仅作稳定引用。"""
    cleaned = value.replace("-", "").replace(":", "")
    return cleaned[:15] if "T" in cleaned else cleaned[:8]


def _ref_of(event: dict[str, Any]) -> str:
    """周期实例按 recurringEventId+originalStart 稳定引用；单次按 id。

    锚点用原始字符串去冒号紧凑形式（保留 Google 给的时区偏移字面量），
    不做时区换算——originalStart 在每次同步中是同一个字面量，跨时区
    换算反而会让 ref 随服务端时区漂移。
    """
    recurring = event.get("recurringEventId")
    if recurring:
        anchor = event.get("originalStart") or _start_raw(event)
        return f"{SOURCE_GOOGLE}:{recurring}#{_anchor_stamp(anchor)}"
    return f"{SOURCE_GOOGLE}:{event.get('id')}"


def _start_raw(event: dict[str, Any]) -> str:
    start = event.get("start") or {}
    return str(start.get("dateTime") or start.get("date") or "")


def _end_raw(event: dict[str, Any]) -> str:
    end = event.get("end") or {}
    return str(end.get("dateTime") or end.get("date") or "")


def _parse_google_time(value: str, *, all_day: bool) -> datetime | None:
    if not value:
        return None
    if all_day and "T" not in value:
        return datetime.fromisoformat(value).replace(tzinfo=DEFAULT_TZ).astimezone(UTC)
    return _to_utc(value)


def sign_state(key: str, user_id: UUID, *, now: datetime | None = None) -> str:
    moment = int((now or datetime.now(UTC)).timestamp())
    message = f"{user_id}:{moment // STATE_TTL_SECONDS}"
    digest = hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()
    return f"{moment}.{digest}"


def verify_state(key: str, state: str, user_id: UUID, *, now: datetime | None = None) -> bool:
    try:
        issued_raw, digest = state.split(".", 1)
        issued = int(issued_raw)
    except (ValueError, AttributeError):
        return False
    moment = now or datetime.now(UTC)
    if abs(moment.timestamp() - issued) > STATE_TTL_SECONDS:
        return False
    expected = hmac.new(
        key.encode(), f"{user_id}:{int(issued) // STATE_TTL_SECONDS}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(digest, expected)


class GoogleTokenStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def save(self, user_id: UUID, refresh_token: str, email: str | None) -> None:
        async with self._database.sessions.begin() as session:
            await self.save_in_session(session, user_id, refresh_token, email)

    async def save_in_session(
        self,
        session: AsyncSession,
        user_id: UUID,
        refresh_token: str,
        email: str | None,
        *,
        authorization_id: UUID | None = None,
    ) -> None:
        owner = await session.scalar(
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
        invalidation = delete(CalendarOAuthStateRecord).where(
            CalendarOAuthStateRecord.user_id == user_id
        )
        if authorization_id is not None:
            invalidation = invalidation.where(CalendarOAuthStateRecord.id != authorization_id)
        await session.execute(invalidation)
        existing = await session.scalar(
            select(CalendarOAuthTokenRecord)
            .where(
                CalendarOAuthTokenRecord.user_id == user_id,
                CalendarOAuthTokenRecord.provider == SOURCE_GOOGLE,
            )
            .with_for_update()
        )
        if existing is not None:
            existing.refresh_token = refresh_token
            existing.account_email = email
            existing.obtained_at = datetime.now(UTC)
        else:
            session.add(
                CalendarOAuthTokenRecord(
                    id=uuid4(),
                    user_id=user_id,
                    provider=SOURCE_GOOGLE,
                    refresh_token=refresh_token,
                    account_email=email,
                    obtained_at=datetime.now(UTC),
                )
            )

    async def get(self, user_id: UUID) -> CalendarOAuthTokenRecord | None:
        async with self._database.sessions() as session:
            return (
                await session.scalars(
                    select(CalendarOAuthTokenRecord).where(
                        CalendarOAuthTokenRecord.user_id == user_id,
                        CalendarOAuthTokenRecord.provider == SOURCE_GOOGLE,
                    )
                )
            ).first()

    async def delete(self, user_id: UUID) -> bool:
        async with self._database.sessions.begin() as session:
            # Disconnect also revokes pending authorization when no token exists.
            await session.execute(
                update(AppUserRecord).where(AppUserRecord.id == user_id).values(id=AppUserRecord.id)
            )
            await session.execute(
                delete(CalendarOAuthStateRecord).where(CalendarOAuthStateRecord.user_id == user_id)
            )
            result = await session.execute(
                delete(CalendarOAuthTokenRecord).where(
                    CalendarOAuthTokenRecord.user_id == user_id,
                    CalendarOAuthTokenRecord.provider == SOURCE_GOOGLE,
                )
            )
        return int(cast(CursorResult[Any], result).rowcount or 0) > 0


class GoogleCalendarClient:
    """刷新令牌 → 访问令牌（进程内缓存）→ Calendar API v3 事件拉取。"""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            transport=httpx.AsyncHTTPTransport(retries=0),
            trust_env=False,
            follow_redirects=False,
        )
        self._access_token: str | None = None
        self._access_expires_at: datetime = datetime.min.replace(tzinfo=UTC)

    def set_source_guard(self, guard: Callable[[], Awaitable[None]]) -> None:
        self._source_guard = guard

    def set_request_runner(self, runner: CalendarRequestRunner) -> None:
        self._request_runner = runner

    async def _send(
        self, method: str, invoke: Callable[[], Awaitable[httpx.Response]]
    ) -> httpx.Response:
        runner = getattr(self, "_request_runner", None)
        return await runner(method, invoke) if runner is not None else await invoke()

    async def _check_source(self) -> None:
        guard = getattr(self, "_source_guard", None)
        if guard is not None:
            await guard()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    async def exchange_code(
        *,
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
        client: httpx.AsyncClient | None = None,
        request_runner: CalendarRequestRunner | None = None,
    ) -> tuple[str, str | None]:
        """Exchange once; the caller owns an injected HTTP client's cleanup."""
        owns = client is None
        http = client or httpx.AsyncClient(
            timeout=15.0,
            transport=httpx.AsyncHTTPTransport(retries=0),
            trust_env=False,
            follow_redirects=False,
        )
        try:
            send = partial(
                http.post,
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
                follow_redirects=False,
            )
            response = (
                await request_runner("POST", send) if request_runner is not None else await send()
            )
            if response.status_code != 200:
                raise GoogleCalendarError("google_code_exchange_failed")
            payload = response.json()
            if not isinstance(payload, dict):
                raise GoogleCalendarError("google_response_invalid")
            refresh = payload.get("refresh_token")
            if not isinstance(refresh, str) or not refresh or len(refresh) > 65536:
                raise GoogleCalendarError("google_no_refresh_token")
            id_token = payload.get("id_token")
            return refresh, _email_from_id_token(id_token) if isinstance(id_token, str) else None
        except httpx.HTTPError as error:
            raise GoogleCalendarError("google_unreachable") from error
        except (ValueError, TypeError) as error:
            raise GoogleCalendarError("google_response_invalid") from error
        finally:
            if owns:
                await join_on_cancel(http.aclose(), name="google-oauth-http-close")

    async def _ensure_access(self) -> str:
        if self._access_token and datetime.now(UTC) < self._access_expires_at:
            return self._access_token
        await self._check_source()
        response = await self._send(
            "POST",
            lambda: self._client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                    "grant_type": "refresh_token",
                },
                follow_redirects=False,
            ),
        )
        if response.status_code in (400, 401):
            raise GoogleCalendarError("google_refresh_token_invalid")
        if response.status_code != 200:
            raise GoogleCalendarError("google_token_failed", str(response.status_code))
        payload = response.json()
        self._access_token = str(payload["access_token"])
        self._access_expires_at = datetime.now(UTC) + timedelta(
            seconds=max(60, int(payload.get("expires_in", 3600)) - 60)
        )
        return self._access_token

    async def list_events(
        self, calendar_id: str, *, start: datetime, end: datetime
    ) -> list[MirrorOccurrence]:
        """singleEvents=true 让 Google 展开周期；窗口内逐次生成镜像。"""
        try:
            return await self._fetch_events(calendar_id, start=start, end=end)
        except httpx.HTTPError as error:
            raise GoogleCalendarError("google_unreachable") from error
        except (ValueError, TypeError, AttributeError) as error:
            raise GoogleCalendarError("google_response_invalid") from error

    async def _fetch_events(
        self, calendar_id: str, *, start: datetime, end: datetime
    ) -> list[MirrorOccurrence]:
        token = await self._ensure_access()
        occurrences: list[MirrorOccurrence] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        for _ in range(MAX_EVENT_PAGES):
            params: dict[str, Any] = {
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": 250,
            }
            if page_token:
                params["pageToken"] = page_token
            await self._check_source()
            response = await self._send(
                "GET",
                partial(
                    self._client.get,
                    GOOGLE_EVENTS_URL.format(calendar_id=calendar_id),
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    follow_redirects=False,
                ),
            )
            if response.status_code == 401:
                raise GoogleCalendarError("google_refresh_token_invalid")
            if response.status_code != 200:
                raise GoogleCalendarError("google_api_failed", str(response.status_code))
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("items", []), list):
                raise GoogleCalendarError("google_response_invalid")
            for event in payload.get("items", []):
                if not isinstance(event, dict):
                    raise GoogleCalendarError("google_response_invalid")
                occurrence = _checked_occurrence(event)
                if occurrence is not None:
                    occurrences.append(occurrence)
            page_token = payload.get("nextPageToken")
            if page_token is None:
                return occurrences
            if not isinstance(page_token, str) or not page_token:
                raise GoogleCalendarError("google_response_invalid")
            if page_token in seen_tokens:
                raise GoogleCalendarError("google_page_cycle")
            seen_tokens.add(page_token)
        raise GoogleCalendarError("google_page_limit")


def _checked_occurrence(event: dict[str, Any]) -> MirrorOccurrence | None:
    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id:
        raise GoogleCalendarError("google_event_invalid")
    cancelled = event.get("status") == "cancelled"
    try:
        if not cancelled and not _end_raw(event):
            raise GoogleCalendarError("google_event_invalid")
        occurrence = _to_occurrence(event)
        if occurrence is None:
            if cancelled:
                return None
            raise GoogleCalendarError("google_event_invalid")
        if len(occurrence.ref) > 160 or occurrence.ends_at <= occurrence.starts_at:
            raise GoogleCalendarError("google_event_invalid")
        return occurrence
    except (ValueError, TypeError, AttributeError) as error:
        raise GoogleCalendarError("google_event_invalid") from error


def _to_occurrence(event: dict[str, Any]) -> MirrorOccurrence | None:
    start_raw = _start_raw(event)
    if not start_raw:
        return None
    all_day = "T" not in start_raw
    starts_at = _parse_google_time(start_raw, all_day=all_day)
    ends_at = _parse_google_time(_end_raw(event), all_day=all_day)
    if starts_at is None:
        return None
    if ends_at is None:
        ends_at = starts_at + timedelta(days=1)
    return MirrorOccurrence(
        ref=_ref_of(event),
        summary=str(event.get("summary") or "(无标题)")[:320],
        starts_at=starts_at,
        ends_at=ends_at,
        all_day=all_day,
        location=(str(event.get("location"))[:240] if event.get("location") else None),
        description=(str(event.get("description"))[:2000] if event.get("description") else None),
        cancelled=event.get("status") == "cancelled",
        etag=str(event.get("etag") or event.get("id") or ""),
    )


def _email_from_id_token(id_token: str) -> str | None:
    import base64
    import json

    try:
        payload_part = id_token.split(".")[1]
        padded = payload_part + "=" * (-len(payload_part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        email = payload.get("email") if isinstance(payload, dict) else None
        return email if isinstance(email, str) and 0 < len(email) <= 254 else None
    except (IndexError, ValueError):
        return None


def build_authorize_url(
    *, client_id: str, redirect_uri: str, state: str, access_type: str = "offline"
) -> str:
    import urllib.parse

    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": _SCOPES,
            "access_type": access_type,
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{GOOGLE_AUTH_URL}?{query}"


class GoogleCalendarSyncService:
    def __init__(
        self,
        database: Database,
        config_store: ConfigStore | DatabaseConfigStore,
        *,
        client_factory: Any = None,
        clock: Any = None,
    ) -> None:
        self._database = database
        self._config_store = config_store
        self._client_factory = client_factory or GoogleCalendarClient
        self._clock = clock or (lambda: datetime.now(UTC))
        self._mirror = CalendarMirrorService(database, source=SOURCE_GOOGLE)
        self._tokens = GoogleTokenStore(database)

    async def default_user_id(self) -> UUID | None:
        async with self._database.sessions() as session:
            value = await session.scalar(
                select(AppUserRecord.id)
                .where(AppUserRecord.status == "active")
                .order_by(AppUserRecord.created_at)
                .limit(1)
            )
        return UUID(str(value)) if value is not None else None

    async def sync_once(
        self,
        *,
        user_id: UUID | None = None,
        actor_id: UUID | None = None,
        source_guard: Callable[[], None] | None = None,
    ) -> SyncStats:
        stats = SyncStats()
        accepted_version = self._config_store.current.version
        config = deepcopy(self._config_store.current.config.integrations.calendar.google)
        secret = resolve_google_secret(config)
        if not config.enabled or config.client_id is None or secret is None:
            stats.errors.append("google_not_configured")
            return stats
        if user_id is None:
            user_id = await self.default_user_id()
        if user_id is None:
            stats.errors.append("no_active_user")
            return stats
        token_record = await self._tokens.get(user_id)
        if token_record is None:
            stats.errors.append("google_not_authorized")
            return stats

        def check_settings() -> None:
            if source_guard is not None:
                source_guard()
            current = self._config_store.current.config.integrations.calendar.google
            if (
                self._config_store.current.version != accepted_version
                or current != config
                or resolve_google_secret(current) != secret
            ):
                raise PermissionError("calendar_sync_source_changed")

        source = CalendarSyncSource(
            self._database,
            owner=user_id,
            settings_guard=check_settings,
            token=token_record,
            actor_id=actor_id,
        )
        await source.check()
        now = self._clock()
        window_start = now - timedelta(days=config.window_days_back)
        window_end = now + timedelta(days=config.window_days_forward)

        client_id = config.client_id

        async def fetch(requests: CalendarRequestRunner) -> CalendarSyncBatch:
            calendar_ids = config.calendar_ids or ["primary"]
            occurrences_by_ref: dict[str, MirrorOccurrence] = {}
            ambiguous_refs: set[str] = set()
            successful_fetches = 0
            for calendar_id in calendar_ids:
                stats.calendars += 1
                await source.check()
                client = self._client_factory(
                    client_id=client_id,
                    client_secret=secret,
                    refresh_token=token_record.refresh_token,
                    timeout_seconds=config.timeout_seconds,
                )
                if isinstance(client, GoogleCalendarClient):
                    client.set_source_guard(source.check)
                    client.set_request_runner(requests)
                try:
                    fetched = deepcopy(
                        await source.call(
                            partial(
                                client.list_events, calendar_id, start=window_start, end=window_end
                            )
                        )
                    )
                except GoogleCalendarError as error:
                    stats.errors.append(f"fetch_failed:{calendar_id}:{error.reason_code}")
                    logger.warning("google calendar fetch failed: %s", error.reason_code)
                    continue
                finally:
                    closer = getattr(client, "close", None)
                    if closer is not None:
                        source.closing = True
                        try:
                            await join_on_cancel(closer(), name="calendar-sync-close")
                        finally:
                            source.closing = False
                successful_fetches += 1
                stats.pulled += len(fetched)
                if merge_calendar_occurrences(
                    occurrences_by_ref,
                    fetched,
                    calendar_id=f"{SOURCE_GOOGLE}:{calendar_id}",
                    ambiguous_refs=ambiguous_refs,
                ):
                    stats.errors.append("calendar_reference_ambiguous")
            return CalendarSyncBatch(
                f"{SOURCE_GOOGLE}:{calendar_ids[0]}"[:64],
                occurrences_by_ref,
                stats,
                successful_fetches,
            )

        batch = await owned_calendar_sync(
            self._database,
            self._config_store,
            user_id=user_id,
            provider="google",
            price=config.sync_cost,
            source=source,
            mirror=self._mirror,
            fetch=fetch,
            now=now,
        )
        return cast(SyncStats, batch.stats)


__all__ = [
    "SOURCE_GOOGLE",
    "GoogleCalendarClient",
    "GoogleCalendarError",
    "GoogleCalendarSyncService",
    "GoogleTokenStore",
    "SyncStats",
    "build_authorize_url",
    "sign_state",
    "verify_state",
]
