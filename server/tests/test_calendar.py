from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import create_calendar_router
from app.auth import AuthService
from app.calendar import CalendarParticipant, CalendarService, CalendarStore
from app.db import AppUserRecord, Base, Database, create_database
from app.ids import uuid7
from app.tasks.models import TaskStatus
from app.tasks.store import TaskStore

NOW = datetime(2026, 9, 2, 2, 0, tzinfo=UTC)  # Asia/Shanghai 当天 10:00


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
        session.add(AppUserRecord(id=value, display_name="Calendar owner", status="active"))
    return value


def _service(
    database: Database,
    *,
    clock: Callable[[], datetime] = lambda: NOW,
) -> CalendarService:
    return CalendarService(CalendarStore(database), TaskStore(database), clock=clock)


# ---------------------------------------------------------------------------
# 冲突检查与预览（写入前展示）
# ---------------------------------------------------------------------------


async def test_preview_reports_conflicts_without_writing(database: Database, user_id: UUID) -> None:
    service = _service(database)
    await service.create_event(
        user_id,
        title="既有会议",
        starts_at=NOW + timedelta(hours=2),
        ends_at=NOW + timedelta(hours=3),
        reminder_lead_minutes=0,
    )
    preview = await service.preview(
        user_id,
        title="重叠的新会议",
        starts_at=NOW + timedelta(hours=2, minutes=30),
        ends_at=NOW + timedelta(hours=4),
        participants=[CalendarParticipant(name="小王")],
    )
    assert len(preview.conflicts) == 1
    assert preview.conflicts[0].title == "既有会议"
    assert preview.participants[0].name == "小王"
    assert preview.calendar_id == "primary"
    # 预览不落库
    events = await service.list_events(user_id)
    assert [event.title for event in events] == ["既有会议"]


async def test_adjacent_events_are_not_conflicts(database: Database, user_id: UUID) -> None:
    service = _service(database)
    await service.create_event(
        user_id,
        title="上一场",
        starts_at=NOW + timedelta(hours=1),
        ends_at=NOW + timedelta(hours=2),
        reminder_lead_minutes=0,
    )
    preview = await service.preview(
        user_id,
        title="紧接着",
        starts_at=NOW + timedelta(hours=2),
        ends_at=NOW + timedelta(hours=3),
    )
    assert preview.conflicts == []


async def test_invalid_window_and_lead_rejected(database: Database, user_id: UUID) -> None:
    service = _service(database)
    with pytest.raises(ValueError, match="结束时间"):
        await service.preview(
            user_id,
            title="倒窗",
            starts_at=NOW + timedelta(hours=2),
            ends_at=NOW + timedelta(hours=1),
        )
    with pytest.raises(ValueError, match="提前量"):
        await service.preview(
            user_id,
            title="超大提前量",
            starts_at=NOW + timedelta(hours=2),
            ends_at=NOW + timedelta(hours=3),
            reminder_lead_minutes=2000,
        )


# ---------------------------------------------------------------------------
# 会前提醒联动 TASK-01
# ---------------------------------------------------------------------------


async def test_create_event_links_reminder_task(database: Database, user_id: UUID) -> None:
    service = _service(database)
    start = NOW + timedelta(hours=25)  # 明天，提前 10 分钟在将来
    event = await service.create_event(
        user_id, title="和小王的评审", starts_at=start, ends_at=start + timedelta(hours=1)
    )
    assert event.reminder_task_id is not None
    tasks = TaskStore(database)
    task = await tasks.get_task(user_id, event.reminder_task_id)
    assert task.status == TaskStatus.ACTIVE
    assert task.source == "calendar"
    assert task.source_ref == f"calendar:{event.id}"
    assert task.next_fire_at == start - timedelta(minutes=10)
    assert "和小王的评审" in task.title


async def test_cancel_event_cancels_linked_reminder(database: Database, user_id: UUID) -> None:
    service = _service(database)
    start = NOW + timedelta(hours=25)
    event = await service.create_event(
        user_id, title="要取消的会", starts_at=start, ends_at=start + timedelta(hours=1)
    )
    tasks = TaskStore(database)
    assert event.reminder_task_id is not None
    active_task = await tasks.get_task(user_id, event.reminder_task_id)
    assert active_task.status == TaskStatus.ACTIVE

    cancelled = await service.cancel_event(user_id, event.id)
    assert cancelled.status == "cancelled"
    task_view = await tasks.get_task(user_id, event.reminder_task_id)
    assert task_view.status == TaskStatus.CANCELLED
    with pytest.raises(ValueError, match="only active"):
        await service.cancel_event(user_id, event.id)


async def test_reschedule_rebuilds_reminder(database: Database, user_id: UUID) -> None:
    service = _service(database)
    start = NOW + timedelta(hours=25)
    event = await service.create_event(
        user_id, title="改期的会", starts_at=start, ends_at=start + timedelta(hours=1)
    )
    tasks = TaskStore(database)
    old_task_id = event.reminder_task_id

    new_start = start + timedelta(days=1)
    updated = await service.reschedule_event(
        user_id, event.id, starts_at=new_start, ends_at=new_start + timedelta(hours=1)
    )
    assert updated.reminder_task_id is not None
    assert updated.reminder_task_id != old_task_id
    old_task = await tasks.get_task(user_id, old_task_id)  # type: ignore[arg-type]
    assert old_task.status == TaskStatus.CANCELLED
    new_task = await tasks.get_task(user_id, updated.reminder_task_id)
    assert new_task.status == TaskStatus.ACTIVE
    assert new_task.next_fire_at == new_start - timedelta(minutes=10)


async def test_soon_event_gets_immediate_reminder(database: Database, user_id: UUID) -> None:
    """事件太近、提前量已过：提醒尽快触发而不是创建失败。"""
    service = _service(database)
    event = await service.create_event(
        user_id,
        title="五分钟后的会",
        starts_at=NOW + timedelta(minutes=5),
        ends_at=NOW + timedelta(minutes=35),
    )
    assert event.reminder_task_id is not None
    tasks = TaskStore(database)
    task = await tasks.get_task(user_id, event.reminder_task_id)
    assert task.next_fire_at is not None
    assert task.next_fire_at > NOW


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


async def test_calendar_api_full_flow(database: Database) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="Calendar user", password="correct horse")
    service = _service(database, clock=lambda: datetime.now(UTC))
    app = FastAPI()
    app.include_router(create_calendar_router(service, auth))
    headers = {"Authorization": f"Bearer {owner.access_token}"}
    start = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    end = (datetime.now(UTC) + timedelta(days=1, hours=1)).isoformat()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.get("/api/v1/calendar/events")
        previewed = await client.post(
            "/api/v1/calendar/events/preview",
            headers=headers,
            json={
                "title": "与产品对齐",
                "starts_at": start,
                "ends_at": end,
                "participants": [{"name": "小王", "email": "xiaowang@example.com"}],
            },
        )
        created = await client.post(
            "/api/v1/calendar/events",
            headers=headers,
            json={
                "title": "与产品对齐",
                "starts_at": start,
                "ends_at": end,
                "reminder_lead_minutes": 30,
            },
        )
        conflict_preview = await client.post(
            "/api/v1/calendar/events/preview",
            headers=headers,
            json={"title": "撞期的会", "starts_at": start, "ends_at": end},
        )
        invalid = await client.post(
            "/api/v1/calendar/events",
            headers=headers,
            json={"title": "倒窗", "starts_at": end, "ends_at": start},
        )
        listed = await client.get("/api/v1/calendar/events", headers=headers)
        event_id = created.json()["id"]
        patched = await client.patch(
            f"/api/v1/calendar/events/{event_id}",
            headers=headers,
            json={"title": "与产品对齐（改题）"},
        )
        cancelled = await client.post(f"/api/v1/calendar/events/{event_id}/cancel", headers=headers)
        missing = await client.post(f"/api/v1/calendar/events/{uuid7()}/cancel", headers=headers)

    assert unauthorized.status_code == 401
    assert previewed.status_code == 200
    assert previewed.json()["conflicts"] == []
    assert created.status_code == 201
    assert created.json()["reminder_task_id"] is not None
    assert conflict_preview.json()["conflicts"][0]["title"] == "与产品对齐"
    assert invalid.status_code == 422
    assert listed.status_code == 200 and len(listed.json()) == 1
    assert patched.json()["title"] == "与产品对齐（改题）"
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["reminder_task_id"] is None
    assert missing.status_code == 404
