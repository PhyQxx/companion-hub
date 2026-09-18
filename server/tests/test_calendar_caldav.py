"""CAL-01 CalDAV 外部日历同步：发现、窗口拉取、周期展开与镜像 upsert。"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from app.calendar import CalDavSyncService
from app.calendar.caldav import CalDavClient
from app.config import ConfigStore
from app.db import AppUserRecord, Base, CalendarEventRecord, Database, create_database
from app.ids import uuid7

NOW = datetime(2026, 9, 16, 4, 0, tzinfo=UTC)
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

YAML_TEMPLATE = """
schema_version: 1
models:
  cloud:
    provider: openai_compatible
    model: dialogue-v1
    base_url: https://models.example/v1
    secret_ref: env:MODEL_API_KEY
    runs_local: false
    max_privacy_level: L1
    max_context_tokens: 32768
    input_cost_per_million: 0
    output_cost_per_million: 0
  local:
    provider: openai_compatible
    model: local-model
    base_url: http://127.0.0.1:11434/v1
    runs_local: true
    max_privacy_level: L2
    max_context_tokens: 32768
    input_cost_per_million: 0
    output_cost_per_million: 0
routes:
  dialogue: {primary: cloud}
  utility: {primary: cloud}
  private: {primary: local}
"""


PROPFIND_RESPONSE = """<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:response>
    <D:href>/calendars/inbox/</D:href>
    <D:propstat><D:prop>
      <D:resourcetype><D:collection/></D:resourcetype>
      <D:displayname>inbox</D:displayname>
    </D:prop></D:propstat>
  </D:response>
  <D:response>
    <D:href>/calendars/personal/</D:href>
    <D:propstat><D:prop>
      <D:resourcetype><D:collection/><C:calendar/></D:resourcetype>
      <D:displayname>个人日历</D:displayname>
    </D:prop></D:propstat>
  </D:response>
</D:multistatus>
"""


def _report_response(events: list[tuple[str, str]]) -> str:
    """events: [(etag, ical_data)]"""
    responses = []
    for etag, ical in events:
        responses.append(
            "<D:response>"
            f"<D:href>/calendars/personal/{etag}.ics</D:href>"
            "<D:propstat><D:prop>"
            f'<D:getetag>"{etag}"</D:getetag>'
            f"<C:calendar-data>{ical}</C:calendar-data>"
            "</D:prop></D:propstat>"
            "</D:response>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        + "".join(responses)
        + "</D:multistatus>"
    )


SINGLE_EVENT = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//CN
BEGIN:VEVENT
UID:single-1
SUMMARY:牙医预约
LOCATION:诊所
DESCRIPTION:带医保卡
DTSTART:20260920T090000Z
DTEND:20260920T100000Z
END:VEVENT
END:VCALENDAR
"""

CANCELLED_EVENT = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//CN
BEGIN:VEVENT
UID:cancelled-1
SUMMARY:已取消的会
STATUS:CANCELLED
DTSTART:20260922T020000Z
DTEND:20260922T030000Z
END:VEVENT
END:VCALENDAR
"""

WEEKLY_EVENT = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//CN
BEGIN:VEVENT
UID:weekly-1
SUMMARY:每周例会
DTSTART:20260914T080000Z
DTEND:20260914T090000Z
RRULE:FREQ=WEEKLY
END:VEVENT
END:VCALENDAR
"""

DAILY_WITH_EXDATE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//CN
BEGIN:VEVENT
UID:daily-exdate-1
SUMMARY:每日站会
DTSTART:20260915T010000Z
DTEND:20260915T013000Z
RRULE:FREQ=DAILY
EXDATE:20260917T010000Z
END:VEVENT
END:VCALENDAR
"""


class FakeCalDav:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = [
            ("etag-single-1", SINGLE_EVENT),
            ("etag-cancelled-1", CANCELLED_EVENT),
            ("etag-weekly-1", WEEKLY_EVENT),
            ("etag-daily-1", DAILY_WITH_EXDATE),
        ]
        self.auth_seen: list[str | None] = []
        self.methods: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.methods.append(request.method)
        self.auth_seen.append(request.headers.get("Authorization"))
        if request.method == "PROPFIND":
            return httpx.Response(207, text=PROPFIND_RESPONSE)
        if request.method == "REPORT":
            return httpx.Response(207, text=_report_response(self.events))
        return httpx.Response(404, text="not found")


async def _config_store(
    tmp_path: Path, *, enabled: bool = True, calendar_names: str = ""
) -> ConfigStore:
    body = YAML_TEMPLATE
    if enabled:
        body += (
            "integrations:\n"
            "  calendar:\n"
            "    caldav:\n"
            "      enabled: true\n"
            "      url: https://caldav.test/\n"
            "      username: aria\n"
            "      secret_value: app-secret\n"
            "      window_days_forward: 21\n"
        )
        if calendar_names:
            body += f"      calendar_names:\n{calendar_names}"
    path = tmp_path / "caldav.yaml"
    path.write_text(body, encoding="utf-8")
    store = ConfigStore(path)
    await store.load()
    return store


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    value = create_database("sqlite+aiosqlite:///:memory:")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield value
    finally:
        await value.close()


@pytest.fixture
async def user_id(database: Database) -> UUID:
    value = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=value, display_name="Cal owner", status="active"))
    return value


def _service(
    database: Database, config_store: ConfigStore, fake: FakeCalDav
) -> CalDavSyncService:
    def factory(
        *, base_url: str, username: str, secret: str, timeout_seconds: float
    ) -> CalDavClient:
        return CalDavClient(
            base_url=base_url,
            username=username,
            secret=secret,
            timeout_seconds=timeout_seconds,
            client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        )

    return CalDavSyncService(database, config_store, client_factory=factory, clock=lambda: NOW)


async def _mirrors(database: Database) -> list[CalendarEventRecord]:
    async with database.sessions() as session:
        records = (
            await session.scalars(
                select(CalendarEventRecord).where(CalendarEventRecord.source == "caldav")
            )
        ).all()
    return list(records)


class TestCalDavSync:
    async def test_disabled_config_short_circuits(
        self, database: Database, tmp_path: Path
    ) -> None:
        store = await _config_store(tmp_path, enabled=False)
        stats = await _service(database, store, FakeCalDav()).sync_once()
        assert stats.errors == ["caldav_not_configured"]
        assert stats.mirrors_created == 0

    async def test_sync_creates_mirrors_with_recurrence_and_exdate(
        self, database: Database, user_id: UUID, tmp_path: Path
    ) -> None:
        fake = FakeCalDav()
        store = await _config_store(tmp_path)
        stats = await _service(database, store, fake).sync_once()

        assert stats.calendars == 1  # inbox 不是日历集合
        assert stats.errors == []
        mirrors = {
            record.source_ref: record
            for record in await _mirrors(database)
            if record.source_ref is not None
        }
        # 单次 1 + 周例会 4 次（14/21/28/1005 在窗口内）+ 日站会 22 次（17 被 EXDATE）
        # 远端已取消的事件不建镜像
        assert len(mirrors) == 27
        single = next(
            r for r in mirrors.values() if r.source_ref == "caldav:single-1#20260920T170000"
        )
        assert single.title == "牙医预约"
        assert single.location == "诊所"
        assert single.calendar_id == "caldav:个人日历"
        assert single.status == "active"
        assert single.external_etag == "etag-single-1"
        assert not any(r.startswith("caldav:cancelled-1") for r in mirrors)
        weekly_refs = sorted(r for r in mirrors if r.startswith("caldav:weekly-1"))
        assert len(weekly_refs) == 4
        daily_refs = sorted(r for r in mirrors if r.startswith("caldav:daily-exdate-1"))
        assert len(daily_refs) == 22
        assert not any("20260917T090000" in ref for ref in daily_refs)
        # Basic 鉴权随请求发送
        assert fake.auth_seen and fake.auth_seen[0] == "Basic " + base64.b64encode(
            b"aria:app-secret"
        ).decode()

    async def test_unchanged_etag_skips_and_update_propagates(
        self, database: Database, user_id: UUID, tmp_path: Path
    ) -> None:
        fake = FakeCalDav()
        store = await _config_store(tmp_path)
        service = _service(database, store, fake)
        await service.sync_once()

        again = await service.sync_once()
        assert again.mirrors_updated == 0
        assert again.mirrors_created == 0

        fake.events = [
            ("etag-single-2", SINGLE_EVENT.replace("牙医预约", "牙医复诊")),
            *fake.events[1:],
        ]
        updated = await service.sync_once()
        assert updated.mirrors_updated == 1
        mirrors = {r.source_ref: r for r in await _mirrors(database)}
        assert mirrors["caldav:single-1#20260920T170000"].title == "牙医复诊"

    async def test_remote_delete_cancels_mirror_but_keeps_local_events(
        self, database: Database, user_id: UUID, tmp_path: Path
    ) -> None:
        fake = FakeCalDav()
        store = await _config_store(tmp_path)
        service = _service(database, store, fake)
        await service.sync_once()

        async with database.sessions.begin() as session:
            session.add(
                CalendarEventRecord(
                    id=uuid7(),
                    user_id=user_id,
                    calendar_id="primary",
                    title="本地事件",
                    starts_at=NOW.replace(tzinfo=None),
                    ends_at=NOW.replace(tzinfo=None),
                    all_day=False,
                    participants=[],
                    status="active",
                    source="api",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

        fake.events = [fake.events[0]]  # 远端只剩 single
        stats = await service.sync_once()
        assert stats.mirrors_cancelled >= 1
        mirrors = await _mirrors(database)
        assert all(
            r.status == "active"
            for r in mirrors
            if r.source_ref == "caldav:single-1#20260920T170000"
        )

        async with database.sessions() as session:
            local = (
                await session.scalars(
                    select(CalendarEventRecord).where(CalendarEventRecord.source == "api")
                )
            ).all()
        assert len(local) == 1 and local[0].status == "active"

    async def test_calendar_name_filter(
        self, database: Database, user_id: UUID, tmp_path: Path
    ) -> None:
        fake = FakeCalDav()
        store = await _config_store(tmp_path, calendar_names="      - 别的日历\n")
        stats = await _service(database, store, fake).sync_once()
        assert stats.calendars == 0
        assert await _mirrors(database) == []

    async def test_auth_failure_maps_to_error(
        self, database: Database, user_id: UUID, tmp_path: Path
    ) -> None:
        class Rejecting(FakeCalDav):
            def handler(self, request: httpx.Request) -> httpx.Response:
                return httpx.Response(401, text="denied")

        store = await _config_store(tmp_path)
        stats = await _service(database, store, Rejecting()).sync_once()
        assert stats.errors == ["caldav_auth_failed"]
