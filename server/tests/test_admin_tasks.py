"""TASK-01 Admin 任务可视化 API：分页/筛选/状态计数与运维取消。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.admin_tasks import create_admin_tasks_router
from app.db import AppUserRecord, Base, Database, create_database
from app.ids import uuid7
from app.tasks import TaskKind, TaskStore, TaskTrigger

AUTH = {"Authorization": "Bearer test-admin-token"}


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


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
def client_app(database: Database) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_admin_tasks_router(
            TaskStore(database),
            admin_token="test-admin-token",
        )
    )
    return app


@pytest.fixture
async def user_id(database: Database) -> UUID:
    value = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=value, display_name="Task owner", status="active"))
    return value


async def _create(
    database: Database,
    user_id: UUID,
    title: str,
    *,
    kind: TaskKind = TaskKind.REMINDER,
    source: str = "manual",
    minutes_ahead: int = 30,
) -> None:
    await TaskStore(database).create(
        user_id=user_id,
        kind=kind,
        title=title,
        trigger=TaskTrigger(type="time", at=datetime.now(UTC) + timedelta(minutes=minutes_ahead)),
        source=source,
    )


class TestAdminTasksList:
    async def test_requires_admin_token(self, client_app: FastAPI) -> None:
        async with _client(client_app) as client:
            response = await client.get("/api/v1/admin/tasks")
        assert response.status_code == 401

    async def test_empty_list_returns_zero_counts(
        self, client_app: FastAPI, user_id: UUID
    ) -> None:
        async with _client(client_app) as client:
            response = await client.get("/api/v1/admin/tasks", headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        assert body["items"] == []
        assert body["total"] == 0
        assert body["counts"] == {}

    async def test_list_pagination_and_counts(
        self, client_app: FastAPI, database: Database, user_id: UUID
    ) -> None:
        for index in range(3):
            await _create(database, user_id, f"提醒 {index}")
        store = TaskStore(database)
        await store.create(
            user_id=user_id,
            kind=TaskKind.TASK,
            title="已完成任务",
            trigger=TaskTrigger(type="time", at=datetime.now(UTC) + timedelta(minutes=5)),
        )
        latest = (await store.list_tasks(user_id))[0]
        await store.complete_task(user_id, latest.id)

        async with _client(client_app) as client:
            page1 = await client.get(
                "/api/v1/admin/tasks", params={"limit": 2, "offset": 0}, headers=AUTH
            )
            page2 = await client.get(
                "/api/v1/admin/tasks", params={"limit": 2, "offset": 2}, headers=AUTH
            )
        assert page1.status_code == 200
        body1 = page1.json()
        assert body1["total"] == 4
        assert len(body1["items"]) == 2
        assert body1["counts"] == {"active": 3, "done": 1}
        # created_at 倒序分页：第二页与第一页不重叠
        ids1 = {item["id"] for item in body1["items"]}
        ids2 = {item["id"] for item in page2.json()["items"]}
        assert not ids1 & ids2

    async def test_filters_by_status_kind_source_and_title(
        self, client_app: FastAPI, database: Database, user_id: UUID
    ) -> None:
        other = uuid7()
        async with database.sessions.begin() as session:
            session.add(AppUserRecord(id=other, display_name="Other", status="active"))
        await _create(database, user_id, "每天喝水", source="chat")
        await _create(database, user_id, "周报提醒", kind=TaskKind.TASK, source="manual")
        await _create(database, other, "别人的提醒", source="chat")

        async with _client(client_app) as client:
            by_title = await client.get(
                "/api/v1/admin/tasks", params={"query": "周报"}, headers=AUTH
            )
            by_kind = await client.get(
                "/api/v1/admin/tasks", params={"kind": "task"}, headers=AUTH
            )
            by_source = await client.get(
                "/api/v1/admin/tasks", params={"source": "chat"}, headers=AUTH
            )
            by_user = await client.get(
                "/api/v1/admin/tasks", params={"user_id": str(other)}, headers=AUTH
            )
            by_status = await client.get(
                "/api/v1/admin/tasks", params={"status": "done"}, headers=AUTH
            )
            invalid = await client.get(
                "/api/v1/admin/tasks", params={"status": "bogus"}, headers=AUTH
            )
        assert [item["title"] for item in by_title.json()["items"]] == ["周报提醒"]
        assert [item["title"] for item in by_kind.json()["items"]] == ["周报提醒"]
        assert {item["title"] for item in by_source.json()["items"]} == {"每天喝水", "别人的提醒"}
        assert [item["title"] for item in by_user.json()["items"]] == ["别人的提醒"]
        assert by_status.json()["total"] == 0
        assert invalid.status_code == 422

    async def test_task_view_carries_trigger_and_delivery_fields(
        self, client_app: FastAPI, database: Database, user_id: UUID
    ) -> None:
        await _create(database, user_id, "事件触发提醒")
        store = TaskStore(database)
        record = (await store.list_tasks(user_id))[0]
        await store.cancel_task(user_id, record.id)

        async with _client(client_app) as client:
            response = await client.get(
                "/api/v1/admin/tasks", params={"status": "cancelled"}, headers=AUTH
            )
        item = response.json()["items"][0]
        assert item["trigger"]["type"] == "time"
        assert item["status"] == "cancelled"
        assert item["source"] == "manual"
        assert item["cancelled_at"] is not None


class TestAdminTaskCancel:
    async def test_cancel_active_task(
        self, client_app: FastAPI, database: Database, user_id: UUID
    ) -> None:
        await _create(database, user_id, "待取消")
        record = (await TaskStore(database).list_tasks(user_id))[0]

        async with _client(client_app) as client:
            response = await client.post(
                f"/api/v1/admin/tasks/{record.id}/cancel",
                params={"user_id": str(user_id)},
                headers=AUTH,
            )
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"

    async def test_cancel_done_task_conflicts(
        self, client_app: FastAPI, database: Database, user_id: UUID
    ) -> None:
        await _create(database, user_id, "先完成")
        store = TaskStore(database)
        record = (await store.list_tasks(user_id))[0]
        await store.complete_task(user_id, record.id)

        async with _client(client_app) as client:
            response = await client.post(
                f"/api/v1/admin/tasks/{record.id}/cancel",
                params={"user_id": str(user_id)},
                headers=AUTH,
            )
        assert response.status_code == 409

    async def test_cancel_unknown_task_404(self, client_app: FastAPI, user_id: UUID) -> None:
        async with _client(client_app) as client:
            response = await client.post(
                f"/api/v1/admin/tasks/{uuid7()}/cancel",
                params={"user_id": str(user_id)},
                headers=AUTH,
            )
        assert response.status_code == 404
