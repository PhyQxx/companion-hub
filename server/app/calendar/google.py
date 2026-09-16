"""CAL-01 Google 日历：OAuth 刷新令牌流 + Calendar API v3 只读镜像。

流程：
1. Admin/用户在 Google Cloud Console 建 OAuth 客户端（Desktop/Web 均可），
   把 client_id/secret 配进 `integrations.calendar.google`，redirect_uri
   填 Hub 的 `/api/v1/calendar/google/callback`；
2. `GET /api/v1/calendar/google/authorize` 生成带签名的 state 并返回
   授权 URL，用户在浏览器完成同意；
3. callback 校验 state、用 code 换 refresh token 并落库
   （`calendar_oauth_token`，按用户+google 唯一）；
4. `GoogleCalendarSyncService` 用刷新令牌拉取时间窗事件
   （`singleEvents=true` 由服务端展开周期），经共享镜像引擎 upsert。

安全：刷新令牌只落库不进日志/配置文档；state 用运行管理令牌 HMAC 签名
（10 分钟有效），callback 无需携带会话凭据也不可被伪造。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult

from app.calendar.mirror import CalendarMirrorService, MirrorOccurrence
from app.config import ConfigStore, DatabaseConfigStore
from app.config.models import GoogleCalendarConfig
from app.db import AppUserRecord, CalendarOAuthTokenRecord, Database

logger = logging.getLogger("app.calendar.google")

SOURCE_GOOGLE = "google"
DEFAULT_TZ = ZoneInfo("Asia/Shanghai")
STATE_TTL_SECONDS = 600
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
            existing = (
                await session.scalars(
                    select(CalendarOAuthTokenRecord).where(
                        CalendarOAuthTokenRecord.user_id == user_id,
                        CalendarOAuthTokenRecord.provider == SOURCE_GOOGLE,
                    )
                )
            ).first()
            if existing is not None:
                existing.refresh_token = refresh_token
                existing.account_email = email
                existing.obtained_at = datetime.now(UTC)
                return
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
        result: Any
        async with self._database.sessions.begin() as session:
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
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._access_token: str | None = None
        self._access_expires_at: datetime = datetime.min.replace(tzinfo=UTC)

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
    ) -> tuple[str, str | None]:
        """authorization code → (refresh_token, account_email)。"""
        owns = client is None
        http = client or httpx.AsyncClient(timeout=15.0)
        try:
            response = await http.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
        finally:
            if owns:
                await http.aclose()
        if response.status_code != 200:
            raise GoogleCalendarError("google_code_exchange_failed", str(response.status_code))
        payload = response.json()
        refresh = payload.get("refresh_token")
        if not refresh:
            raise GoogleCalendarError("google_no_refresh_token")
        return str(refresh), payload.get("id_token") and _email_from_id_token(
            str(payload.get("id_token"))
        )

    async def _ensure_access(self) -> str:
        if self._access_token and datetime.now(UTC) < self._access_expires_at:
            return self._access_token
        response = await self._client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            },
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
        token = await self._ensure_access()
        occurrences: list[MirrorOccurrence] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": 250,
            }
            if page_token:
                params["pageToken"] = page_token
            response = await self._client.get(
                GOOGLE_EVENTS_URL.format(calendar_id=calendar_id),
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code == 401:
                raise GoogleCalendarError("google_refresh_token_invalid")
            if response.status_code != 200:
                raise GoogleCalendarError("google_api_failed", str(response.status_code))
            payload = response.json()
            for event in payload.get("items", []):
                occurrence = _to_occurrence(event)
                if occurrence is not None:
                    occurrences.append(occurrence)
            page_token = payload.get("nextPageToken")
            if not page_token:
                return occurrences


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
        description=(
            str(event.get("description"))[:2000] if event.get("description") else None
        ),
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
        email = payload.get("email")
        return str(email) if email else None
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

    async def sync_once(self) -> SyncStats:
        stats = SyncStats()
        config = self._config_store.current.config.integrations.calendar.google
        secret = resolve_google_secret(config)
        if not config.enabled or config.client_id is None or secret is None:
            stats.errors.append("google_not_configured")
            return stats
        user_id = await self.default_user_id()
        if user_id is None:
            stats.errors.append("no_active_user")
            return stats
        token_record = await self._tokens.get(user_id)
        if token_record is None:
            stats.errors.append("google_not_authorized")
            return stats
        now = self._clock()
        window_start = now - timedelta(days=config.window_days_back)
        window_end = now + timedelta(days=config.window_days_forward)
        calendar_ids = config.calendar_ids or ["primary"]
        occurrences_by_ref: dict[str, MirrorOccurrence] = {}
        for calendar_id in calendar_ids:
            stats.calendars += 1
            client = self._client_factory(
                client_id=config.client_id,
                client_secret=secret,
                refresh_token=token_record.refresh_token,
                timeout_seconds=config.timeout_seconds,
            )
            try:
                fetched = await client.list_events(calendar_id, start=window_start, end=window_end)
            except GoogleCalendarError as error:
                stats.errors.append(f"fetch_failed:{calendar_id}:{error.reason_code}")
                logger.warning("google calendar fetch failed for %s: %s", calendar_id, error)
                continue
            finally:
                closer = getattr(client, "close", None)
                if closer is not None:
                    await closer()
            stats.pulled += len(fetched)
            for occurrence in fetched:
                occurrences_by_ref[occurrence.ref] = occurrence
        await self._mirror.apply(
            user_id,
            f"{SOURCE_GOOGLE}:{calendar_ids[0]}"[:64],
            occurrences_by_ref,
            stats=stats,
            now=now,
        )
        return stats


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
