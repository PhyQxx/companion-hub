"""CAL-01 CalDAV 客户端与镜像同步引擎。

协议（RFC 4791 最小子集，httpx 异步实现）：
- PROPFIND Depth:1 发现日历集合（resourcetype 含 calendar）；
- REPORT calendar-query + time-range 拉取窗口内事件（含 getetag）；
- VEVENT 解析交给 icalendar；周期事件在窗口内用 dateutil.rrule 展开，
  RECURRENCE-ID 覆盖特定次，EXDATE 剔除。

同步语义（与 TODO-01 pnkx 镜像同构，但只读不回写）：
- 按 source_ref=caldav:{uid}[#{recurrence-id}] upsert 镜像；
- 远端 etag 未变跳过；远端删除/窗口滑出 → 本地镜像取消；
- 镜像不创建本地提醒任务（外部日历自带通知）；
- 本地（source!=caldav）事件永不触碰。
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from dateutil.rrule import rrulestr
from icalendar import Calendar
from sqlalchemy import select

from app.calendar.mirror import CalendarMirrorService, MirrorOccurrence
from app.config import ConfigStore, DatabaseConfigStore
from app.config.models import CalDavConfig
from app.db import AppUserRecord, Database

logger = logging.getLogger("app.calendar.caldav")

SOURCE_CALDAV = "caldav"
DEFAULT_TZ = ZoneInfo("Asia/Shanghai")

_PROPFIND_BODY = """<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop>
    <D:resourcetype/>
    <D:displayname/>
  </D:prop>
</D:propfind>
"""


def _report_body(start: datetime, end: datetime) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop>
    <D:getetag/>
    <C:calendar-data/>
  </D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT">
        <C:time-range start="{_caldav_stamp(start)}" end="{_caldav_stamp(end)}"/>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>
"""


def _caldav_stamp(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


class CalDavError(RuntimeError):
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
    skipped_recurring_unsupported: int = 0
    errors: list[str] = field(default_factory=list)


def resolve_caldav_secret(config: CalDavConfig) -> str | None:
    import os

    if config.secret_value is not None:
        return config.secret_value
    if config.secret_ref is not None:
        return os.environ.get(config.secret_ref.removeprefix("env:"))
    return None


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=DEFAULT_TZ).astimezone(UTC)
    return value.astimezone(UTC)


class CalDavClient:
    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        secret: str,
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = (username, secret)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def list_calendars(self) -> list[tuple[str, str]]:
        """返回 [(href, displayname)]；仅包含 resourcetype 为 calendar 的集合。"""
        response = await self._request("PROPFIND", self._base_url, _PROPFIND_BODY, depth="1")
        root = ET.fromstring(response.text)
        results: list[tuple[str, str]] = []
        for response_node in root.findall(".//{DAV:}response"):
            href = response_node.findtext("{DAV:}href", "")
            if not href:
                continue
            resourcetype = response_node.find(".//{DAV:}resourcetype")
            calendar_node = (
                resourcetype.find("{urn:ietf:params:xml:ns:caldav}calendar")
                if resourcetype is not None
                else None
            )
            if calendar_node is None:
                continue
            name = (
                response_node.findtext(".//{DAV:}displayname")
                or href.rstrip("/").split("/")[-1]
            )
            results.append((href, name))
        return results

    async def fetch_window(
        self, href: str, *, start: datetime, end: datetime
    ) -> list[MirrorOccurrence]:
        """REPORT 拉取窗口内事件并展开周期；一个 UID 可产生多个 occurrence。"""
        url = self._url(href)
        response = await self._request("REPORT", url, _report_body(start, end))
        root = ET.fromstring(response.text)
        occurrences: list[MirrorOccurrence] = []
        for response_node in root.findall(".//{DAV:}response"):
            href_item = response_node.findtext("{DAV:}href", "")
            etag = (response_node.findtext(".//{DAV:}getetag") or "").strip().strip('"')
            data = response_node.findtext(
                ".//{urn:ietf:params:xml:ns:caldav}calendar-data"
            )
            if not data or not href_item:
                continue
            occurrences.extend(
                _expand_event(data, etag=etag or href_item, window_start=start, window_end=end)
            )
        return occurrences

    def _url(self, href: str) -> str:
        if href.startswith("http://") or href.startswith("https://"):
            return href
        origin = self._base_url.split("/", 3)
        base = "/".join(origin[:3])
        return f"{base}{href}" if href.startswith("/") else f"{self._base_url}/{href.lstrip('./')}"

    async def _request(
        self, method: str, url: str, body: str, *, depth: str = "0"
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method,
                url,
                content=body,
                headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
                auth=self._auth,
            )
        except httpx.HTTPError as error:
            raise CalDavError("caldav_unreachable", str(error)) from error
        if response.status_code in (401, 403):
            raise CalDavError("caldav_auth_failed")
        if response.status_code >= 400:
            raise CalDavError("caldav_server_error", f"HTTP {response.status_code}")
        return response


def _expand_event(
    ical_data: str, *, etag: str, window_start: datetime, window_end: datetime
) -> list[MirrorOccurrence]:
    """解析单个 VEVENT 资源；周期规则在窗口内展开，覆盖/剔除按 RECURRENCE-ID/EXDATE。"""
    calendar = Calendar.from_ical(ical_data)
    components = [comp for comp in calendar.walk("VEVENT")]
    if not components:
        return []
    master = components[0]
    overrides = {
        str(comp.get("RECURRENCE-ID").dt)
        for comp in components[1:]
        if comp.get("RECURRENCE-ID")
    }
    uid = str(master.get("UID", "")) or etag
    cancelled = str(master.get("STATUS", "")).upper() == "CANCELLED"
    summary = str(master.get("SUMMARY", "") or "(无标题)")[:320]
    location = str(master.get("LOCATION"))[:240] if master.get("LOCATION") else None
    description = str(master.get("DESCRIPTION"))[:2000] if master.get("DESCRIPTION") else None

    dtstart = master.get("DTSTART").dt
    all_day = not isinstance(dtstart, datetime)
    dtend_prop = master.get("DTEND")
    duration = master.get("DURATION")
    if dtend_prop is not None:
        end_value = dtend_prop.dt
        duration = (end_value - dtstart) if isinstance(end_value, datetime) else timedelta(days=1)
    elif duration is not None:
        duration = duration.dt if hasattr(duration, "dt") else duration
    else:
        duration = timedelta(days=1) if all_day else timedelta(hours=1)

    rrule_prop = master.get("RRULE")
    raw_exdates = master.get("EXDATE", [])
    if not isinstance(raw_exdates, list):
        raw_exdates = [raw_exdates]
    exdates = {
        _exdate_key(value.dt if hasattr(value, "dt") else value)
        for prop in raw_exdates
        for value in prop.dts
    }

    def _occurrence(start_value: Any) -> MirrorOccurrence | None:
        start_dt = _as_datetime(start_value)
        end_dt = start_dt + (duration if isinstance(duration, timedelta) else timedelta(hours=1))
        if end_dt < window_start or start_dt > window_end:
            return None
        if _exdate_key(start_value) in exdates:
            return None
        key = _override_key(start_value)
        if key in overrides:
            return None  # 该次由 override 资源自己提供
        return MirrorOccurrence(
            ref=_ref_of(uid, start_value),
            summary=summary,
            starts_at=_to_utc(start_dt),
            ends_at=_to_utc(end_dt),
            all_day=all_day,
            location=location,
            description=description,
            cancelled=cancelled,
            etag=etag,
        )

    results = [_occurrence(dtstart)]
    if rrule_prop is not None:
        try:
            rule = rrulestr(rrule_prop.to_ical().decode(), dtstart=_as_datetime(dtstart))
            for occurrence in rule.between(
                window_start - timedelta(days=1), window_end + timedelta(days=1), inc=True
            ):
                candidate = _occurrence(occurrence)
                if candidate is not None and candidate.ref != _ref_of(uid, dtstart):
                    results.append(candidate)
        except (ValueError, TypeError) as error:
            logger.debug("caldav rrule expand failed for %s: %s", uid, error)
    # override 资源自身也产出 occurrence
    for comp in components[1:]:
        recurrence_id = comp.get("RECURRENCE-ID")
        if recurrence_id is None:
            continue
        override_start = recurrence_id.dt
        override_dt = _as_datetime(override_start)
        override_end = (
            comp.get("DTEND").dt if comp.get("DTEND") else override_dt + timedelta(hours=1)
        )
        if not isinstance(override_end, datetime):
            override_end = override_dt + timedelta(days=1)
        if _to_utc(override_end) < window_start or _to_utc(override_dt) > window_end:
            continue
        results.append(
            MirrorOccurrence(
                ref=_ref_of(uid, override_start),
                summary=str(comp.get("SUMMARY", "") or summary)[:320],
                starts_at=_to_utc(override_dt),
                ends_at=_to_utc(override_end),
                all_day=all_day,
                location=str(comp.get("LOCATION"))[:240] if comp.get("LOCATION") else location,
                description=(
                    str(comp.get("DESCRIPTION"))[:2000]
                    if comp.get("DESCRIPTION")
                    else description
                ),
                cancelled=str(comp.get("STATUS", "")).upper() == "CANCELLED" or cancelled,
                etag=etag,
            )
        )
    return [item for item in results if item is not None]


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=DEFAULT_TZ)
    from datetime import date as _date

    if isinstance(value, _date):
        return datetime(value.year, value.month, value.day, tzinfo=DEFAULT_TZ)
    raise TypeError(f"unsupported datetime value: {value!r}")


def _exdate_key(value: Any) -> str:
    dt = _as_datetime(getattr(value, "dt", value))
    return dt.astimezone(DEFAULT_TZ).isoformat()


def _override_key(value: Any) -> str:
    return _exdate_key(value)


def _ref_of(uid: str, occurrence_start: Any) -> str:
    dt = _as_datetime(getattr(occurrence_start, "dt", occurrence_start))
    stamp = dt.astimezone(DEFAULT_TZ).strftime("%Y%m%dT%H%M%S")
    return f"{SOURCE_CALDAV}:{uid}#{stamp}" if stamp else f"{SOURCE_CALDAV}:{uid}"


class CalDavSyncService:
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
        self._client_factory = client_factory or CalDavClient
        self._clock = clock or (lambda: datetime.now(UTC))
        self._mirror = CalendarMirrorService(database, source=SOURCE_CALDAV)

    async def enabled(self) -> bool:
        config = self._config_store.current.config.integrations.calendar.caldav
        return bool(config.enabled and resolve_caldav_secret(config) is not None)

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
        config = self._config_store.current.config.integrations.calendar.caldav
        secret = resolve_caldav_secret(config)
        if not config.enabled or config.url is None or secret is None:
            stats.errors.append("caldav_not_configured")
            return stats
        user_id = await self.default_user_id()
        if user_id is None:
            stats.errors.append("no_active_user")
            return stats
        now = self._clock()
        window_start = now - timedelta(days=config.window_days_back)
        window_end = now + timedelta(days=config.window_days_forward)

        client = self._client_factory(
            base_url=config.url,
            username=config.username or "",
            secret=secret,
            timeout_seconds=config.timeout_seconds,
        )
        try:
            try:
                calendars = await client.list_calendars()
            except CalDavError as error:
                stats.errors.append(error.reason_code)
                logger.warning("caldav discovery failed: %s", error)
                return stats

            wanted = set(config.calendar_names)
            occurrences_by_ref: dict[str, MirrorOccurrence] = {}
            calendar_slug = "caldav"
            for href, name in calendars:
                if wanted and name not in wanted:
                    continue
                stats.calendars += 1
                try:
                    fetched = await client.fetch_window(
                        href, start=window_start, end=window_end
                    )
                except CalDavError as error:
                    stats.errors.append(f"fetch_failed:{name}:{error.reason_code}")
                    logger.warning("caldav fetch failed for %s: %s", name, error)
                    continue
                stats.pulled += len(fetched)
                calendar_slug = f"{SOURCE_CALDAV}:{_slugify(name)}"
                for occurrence in fetched:
                    occurrences_by_ref[occurrence.ref] = occurrence

            await self._mirror.apply(
                user_id,
                calendar_slug[:64],
                occurrences_by_ref,
                stats=stats,
                now=self._clock(),
            )
            return stats
        finally:
            closer = getattr(client, "close", None)
            if closer is not None:
                await closer()

def _slugify(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name.strip())
    return cleaned.strip("-")[:32] or "default"


__all__ = [
    "SOURCE_CALDAV",
    "CalDavClient",
    "CalDavError",
    "CalDavSyncService",
    "MirrorOccurrence",
    "SyncStats",
]
