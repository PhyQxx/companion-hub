"""CAL-01 Google 日历：state 签名、令牌存储、事件映射与镜像同步。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.calendar import create_calendar_router
from app.auth import AuthService
from app.calendar import CalendarService, CalendarStore
from app.calendar.google import (
    GoogleCalendarClient,
    GoogleCalendarError,
    GoogleCalendarSyncService,
    GoogleTokenStore,
    _to_occurrence,
    sign_state,
    verify_state,
)
from app.config import ConfigStore
from app.db import (
    AppUserRecord,
    Base,
    CalendarEventRecord,
    Database,
    TaskItemRecord,
    create_database,
)
from app.ids import uuid7
from app.tasks.models import TaskStatus
from app.tasks.store import TaskStore

NOW = datetime(2026, 9, 16, 4, 0, tzinfo=UTC)

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
integrations:
  calendar:
    google:
      enabled: true
      client_id: test-client-id.apps.googleusercontent.com
      secret_value: google-secret
      redirect_uri: http://127.0.0.1:8000/api/v1/calendar/google/callback
      window_days_forward: 30
"""


async def _config_store(tmp_path: Path) -> ConfigStore:
    path = tmp_path / "google.yaml"
    path.write_text(YAML_TEMPLATE, encoding="utf-8")
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
        session.add(AppUserRecord(id=value, display_name="G owner", status="active"))
    return value


class TestStateSignature:
    def test_roundtrip_and_expiry(self, user_id: UUID) -> None:
        state = sign_state("secret-key", user_id, now=NOW)
        assert verify_state("secret-key", state, user_id, now=NOW)
        # 不同窗口期过期
        assert not verify_state("secret-key", state, user_id, now=NOW + timedelta(hours=1))
        # 密钥不符
        assert not verify_state("other-key", state, user_id, now=NOW)
        # 用户不符
        assert not verify_state("secret-key", state, uuid7(), now=NOW)
        # 畸形 state
        assert not verify_state("secret-key", "garbage", user_id, now=NOW)


class TestTokenStore:
    async def test_save_get_update_delete(self, database: Database, user_id: UUID) -> None:
        store = GoogleTokenStore(database)
        await store.save(user_id, "refresh-1", "me@example.com")
        record = await store.get(user_id)
        assert record is not None and record.refresh_token == "refresh-1"
        assert record.account_email == "me@example.com"

        await store.save(user_id, "refresh-2", None)
        updated = await store.get(user_id)
        assert updated is not None and updated.refresh_token == "refresh-2"

        assert await store.delete(user_id) is True
        assert await store.get(user_id) is None
        assert await store.delete(user_id) is False

    async def test_user_isolation(self, database: Database, user_id: UUID) -> None:
        store = GoogleTokenStore(database)
        await store.save(user_id, "refresh-a", None)
        other = uuid7()
        async with database.sessions.begin() as session:
            session.add(AppUserRecord(id=other, display_name="Other", status="active"))
        await store.save(other, "refresh-b", None)
        assert (await store.get(user_id)).refresh_token == "refresh-a"  # type: ignore[union-attr]
        assert (await store.get(other)).refresh_token == "refresh-b"  # type: ignore[union-attr]


def test_occurrence_mapping() -> None:
    single = {
        "id": "evt-1",
        "etag": '"e1"',
        "summary": "牙医",
        "location": "诊所",
        "start": {"dateTime": "2026-09-20T17:00:00+08:00"},
        "end": {"dateTime": "2026-09-20T18:00:00+08:00"},
    }
    occurrence = _to_occurrence(single)
    assert occurrence is not None
    assert occurrence.ref == "google:evt-1"
    assert occurrence.summary == "牙医"
    assert occurrence.starts_at.isoformat() == "2026-09-20T09:00:00+00:00"
    assert occurrence.cancelled is False

    cancelled = _to_occurrence({**single, "status": "cancelled"})
    assert cancelled is not None and cancelled.cancelled is True

    recurring = _to_occurrence(
        {
            **single,
            "id": "evt-instance",
            "recurringEventId": "evt-series",
            "originalStart": "2026-09-27T17:00:00+08:00",
        }
    )
    assert recurring is not None
    assert recurring.ref == "google:evt-series#20260927T170000"

    all_day = _to_occurrence(
        {
            "id": "evt-day",
            "etag": '"e3"',
            "summary": "全天",
            "start": {"date": "2026-10-01"},
            "end": {"date": "2026-10-02"},
        }
    )
    assert all_day is not None and all_day.all_day is True


class FakeGoogleHttp:
    """token + events 两端点的 MockTransport。"""

    def __init__(self, events: list[dict[str, object]], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            if self.fail:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={"access_token": "at-1", "expires_in": 3600},
            )
        return httpx.Response(200, json={"items": self.events, "nextPageToken": None})


def _service(
    database: Database, config_store: ConfigStore, fake: FakeGoogleHttp
) -> GoogleCalendarSyncService:
    def factory(**kwargs: object) -> GoogleCalendarClient:
        return GoogleCalendarClient(
            client_id=str(kwargs["client_id"]),
            client_secret=str(kwargs["client_secret"]),
            refresh_token=str(kwargs["refresh_token"]),
            timeout_seconds=cast(float, kwargs.get("timeout_seconds", 15.0)),
            client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        )

    return GoogleCalendarSyncService(
        database, config_store, client_factory=factory, clock=lambda: NOW
    )


class TestGoogleSync:
    async def test_not_authorized_short_circuits(
        self, database: Database, user_id: UUID, tmp_path: Path
    ) -> None:
        store = await _config_store(tmp_path)
        stats = await _service(database, store, FakeGoogleHttp([])).sync_once()
        assert stats.errors == ["google_not_authorized"]

    async def test_sync_creates_mirrors_and_reuses_etag(
        self, database: Database, user_id: UUID, tmp_path: Path
    ) -> None:
        fake = FakeGoogleHttp(
            [
                {
                    "id": "evt-1",
                    "etag": '"e1"',
                    "summary": "周会",
                    "start": {"dateTime": "2026-09-20T10:00:00Z"},
                    "end": {"dateTime": "2026-09-20T11:00:00Z"},
                },
                {
                    "id": "evt-cancelled",
                    "etag": '"e2"',
                    "summary": "已取消",
                    "status": "cancelled",
                    "start": {"dateTime": "2026-09-21T10:00:00Z"},
                    "end": {"dateTime": "2026-09-21T11:00:00Z"},
                },
            ]
        )
        store = await _config_store(tmp_path)
        await GoogleTokenStore(database).save(user_id, "refresh-1", None)
        service = _service(database, store, fake)
        stats = await service.sync_once()
        assert stats.mirrors_created == 1  # 已取消不建镜像
        assert stats.errors == []

        # 第二轮 etag 未变：零更新
        again = await service.sync_once()
        assert again.mirrors_updated == 0
        assert again.mirrors_created == 0

    async def test_refresh_failure_maps_to_error(
        self, database: Database, user_id: UUID, tmp_path: Path
    ) -> None:
        fake = FakeGoogleHttp([], fail=True)
        store = await _config_store(tmp_path)
        await GoogleTokenStore(database).save(user_id, "stale", None)
        stats = await _service(database, store, fake).sync_once()
        assert any("google_refresh_token_invalid" in error for error in stats.errors)

    async def test_code_exchange_and_error_mapping(self) -> None:
        # mock 端点只回 access_token（无 refresh_token）→ 契约上必须拒绝
        ok = FakeGoogleHttp([])
        with pytest.raises(GoogleCalendarError, match="google_no_refresh_token") as no_refresh:
            await GoogleCalendarClient.exchange_code(
                client_id="cid",
                client_secret="secret",
                code="good",
                redirect_uri="http://127.0.0.1/cb",
                client=httpx.AsyncClient(transport=httpx.MockTransport(ok.handler)),
            )
        assert no_refresh.value.reason_code == "google_no_refresh_token"
        bad = FakeGoogleHttp([], fail=True)
        with pytest.raises(GoogleCalendarError) as exchange_failed:
            await GoogleCalendarClient.exchange_code(
                client_id="cid",
                client_secret="secret",
                code="bad",
                redirect_uri="http://127.0.0.1/cb",
                client=httpx.AsyncClient(transport=httpx.MockTransport(bad.handler)),
            )
        assert exchange_failed.value.reason_code == "google_code_exchange_failed"


class TestGoogleDisconnect:
    async def test_disconnect_cancels_mirrors_and_linked_tasks(
        self, database: Database, user_id: UUID, tmp_path: Path
    ) -> None:
        store = await _config_store(tmp_path)
        auth = AuthService(database)
        owner = await auth.setup(display_name="Disconnect owner", password="correct horse")
        google = _service(database, store, FakeGoogleHttp([]))
        service = CalendarService(CalendarStore(database), TaskStore(database))

        mirror_active = CalendarEventRecord(
            id=uuid7(),
            user_id=owner.principal.user_id,
            title="远端会议",
            starts_at=NOW,
            ends_at=NOW + timedelta(hours=1),
            status="active",
            source="google",
            source_ref="google:evt-1",
            created_at=NOW,
            updated_at=NOW,
        )
        mirror_cancelled = CalendarEventRecord(
            id=uuid7(),
            user_id=owner.principal.user_id,
            title="早已取消",
            starts_at=NOW,
            ends_at=NOW + timedelta(hours=1),
            status="cancelled",
            source="google",
            source_ref="google:evt-2",
            created_at=NOW,
            updated_at=NOW,
        )
        local_event = CalendarEventRecord(
            id=uuid7(),
            user_id=owner.principal.user_id,
            title="本地事件不受影响",
            starts_at=NOW,
            ends_at=NOW + timedelta(hours=1),
            status="active",
            source="api",
            source_ref=None,
            created_at=NOW,
            updated_at=NOW,
        )
        reminder = TaskItemRecord(
            id=uuid7(),
            user_id=owner.principal.user_id,
            title="会议提醒",
            status=str(TaskStatus.ACTIVE),
            trigger_type="time",
            trigger_config={"type": "time", "at": "2026-09-16T04:30:00+00:00"},
            source_ref=f"calendar:{mirror_active.id}",
            next_fire_at=NOW + timedelta(minutes=30),
            created_at=NOW,
            updated_at=NOW,
        )
        unrelated_task = TaskItemRecord(
            id=uuid7(),
            user_id=owner.principal.user_id,
            title="无关任务",
            status=str(TaskStatus.ACTIVE),
            trigger_type="time",
            trigger_config={"type": "time", "at": "2026-09-16T04:30:00+00:00"},
            source_ref="calendar:other",
            next_fire_at=NOW + timedelta(minutes=30),
            created_at=NOW,
            updated_at=NOW,
        )
        async with database.sessions.begin() as session:
            session.add_all(
                [mirror_active, mirror_cancelled, local_event, reminder, unrelated_task]
            )
        await GoogleTokenStore(database).save(owner.principal.user_id, "refresh-1", None)

        app = FastAPI()
        app.include_router(
            create_calendar_router(service, auth, google_sync=google, google_state_key="k")
        )
        headers = {"Authorization": f"Bearer {owner.access_token}"}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.delete("/api/v1/calendar/google/token", headers=headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body == {"removed": True, "previews_cancelled": 0, "mirrors_cancelled": 1}

        async with database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(CalendarEventRecord).where(
                        CalendarEventRecord.user_id == owner.principal.user_id
                    )
                )
            )
        by_id = {str(row.id): row for row in rows}
        assert by_id[str(mirror_active.id)].status == "cancelled"
        assert by_id[str(mirror_cancelled.id)].status == "cancelled"  # 原状，不重复计数
        assert by_id[str(local_event.id)].status == "active"

        tasks = TaskStore(database)
        cancelled = await tasks.get_task(owner.principal.user_id, reminder.id)
        assert cancelled.status == TaskStatus.CANCELLED
        untouched = await tasks.get_task(owner.principal.user_id, unrelated_task.id)
        assert untouched.status == TaskStatus.ACTIVE
