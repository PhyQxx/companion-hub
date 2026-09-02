from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import create_todo_router
from app.auth import AuthService
from app.db import AppUserRecord, Base, Database, TaskItemRecord, create_database
from app.ids import uuid7
from app.todo import PnkxTodoClient, TodoSyncService
from app.todo.sync_scheduler import TodoSyncScheduler

NOW = datetime(2026, 9, 2, 4, 0, tzinfo=UTC)


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
        session.add(AppUserRecord(id=value, display_name="Todo owner", status="active"))
    return value


class FakePnkx:
    """按 pnkx 契约响应的 MockTransport：登录、分页 list、create、update。"""

    def __init__(self) -> None:
        self.items: dict[int, dict[str, object]] = {}
        self._next_id = 100
        self.created_bodies: list[dict[str, object]] = []
        self.updated_bodies: list[dict[str, object]] = []

    def add_item(self, **kwargs: object) -> int:
        external_id = self._next_id
        self._next_id += 1
        self.items[external_id] = {
            "id": external_id,
            "content": "任务",
            "status": False,
            "priority": None,
            "label": None,
            "planStartTime": None,
            "planEndTime": None,
            "kanbanStatus": None,
            "clientUuid": None,
            "parentId": 0,
            "updateTime": "2026-09-01 10:00:00",
            "createTime": "2026-09-01 10:00:00",
            **kwargs,
        }
        return external_id

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        token = request.headers.get("X-Integration-Token")
        if token != "integration-secret":
            return httpx.Response(401, json={"code": 401, "msg": "integration token rejected"})
        if path == "/admin/toDo/list":
            rows = list(self.items.values())
            return httpx.Response(200, json={"code": 200, "rows": rows, "total": len(rows)})
        if path == "/admin/toDo" and request.method == "POST":
            body = json.loads(request.content)
            self.created_bodies.append(body)
            external_id = self.add_item(**body)
            return httpx.Response(200, json={"code": 200, "data": external_id})
        if path == "/admin/toDo" and request.method == "PUT":
            body = json.loads(request.content)
            self.updated_bodies.append(body)
            target = self.items[int(body["id"])]
            for key, value in body.items():
                if key != "id":
                    target[key] = value
            return httpx.Response(200, json={"code": 200, "msg": "ok"})
        return httpx.Response(404, json={"code": 404, "msg": "no route"})


def _service(database: Database, fake: FakePnkx) -> TodoSyncService:
    client = PnkxTodoClient(
        base_url="https://pnkx.test",
        integration_token="integration-secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    return TodoSyncService(database, client, clock=lambda: NOW)


async def _mirrors(database: Database, user_id: UUID) -> list[TaskItemRecord]:
    from sqlalchemy import select

    async with database.sessions() as session:
        records = (
            await session.scalars(select(TaskItemRecord).where(TaskItemRecord.user_id == user_id))
        ).all()
    return list(records)


# ---------------------------------------------------------------------------
# 客户端协议
# ---------------------------------------------------------------------------


async def test_client_sends_integration_token_and_lists() -> None:
    fake = FakePnkx()
    fake.add_item(content="A")
    fake.add_item(content="B")
    service_client = PnkxTodoClient(
        base_url="https://pnkx.test",
        integration_token="integration-secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    items = await service_client.list_all()
    await service_client.close()
    # 无有效 X-Integration-Token 的请求会被 FakePnkx 拒绝（401），
    # 能拿到列表即证明令牌头随每个请求发送
    assert [item.content for item in items] == ["A", "B"]
    assert items[0].external_id == "100"


async def test_client_rejected_token_raises() -> None:
    fake = FakePnkx()
    fake.add_item(content="A")
    service_client = PnkxTodoClient(
        base_url="https://pnkx.test",
        integration_token="wrong-token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    with pytest.raises(Exception, match="integration_token_rejected"):
        await service_client.list_all()
    await service_client.close()


async def test_client_create_and_update_payloads() -> None:
    fake = FakePnkx()
    service_client = PnkxTodoClient(
        base_url="https://pnkx.test",
        integration_token="integration-secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    external_id = await service_client.create(
        content="新任务",
        client_uuid="aria:abc",
        priority=3,
        label="工作",
    )
    await service_client.update(
        external_id, status=True, finish_time=datetime(2026, 9, 2, 3, 0, tzinfo=UTC)
    )
    await service_client.close()
    assert fake.created_bodies[0]["clientUuid"] == "aria:abc"
    assert "planStartTime" not in fake.created_bodies[0]  # 未给时间不发送该字段
    assert fake.updated_bodies[0]["status"] is True
    assert fake.updated_bodies[0]["finishTime"] == "2026-09-02 11:00:00"


# ---------------------------------------------------------------------------
# 同步引擎
# ---------------------------------------------------------------------------


async def test_pull_creates_mirrors_and_skips_subtasks(database: Database, user_id: UUID) -> None:
    fake = FakePnkx()
    top = fake.add_item(content="顶层任务", priority=3, label="工作")
    fake.add_item(content="子任务", parentId=top)
    done = fake.add_item(content="已完成", status=True)

    stats = await _service(database, fake).sync_once()
    mirrors = await _mirrors(database, user_id)
    by_ref = {record.source_ref: record for record in mirrors}
    assert stats.pulled == 3  # 拉到 3 条（含子任务），镜像只建 2 条
    assert stats.mirrors_created == 2
    assert set(by_ref) == {f"pnkx:{top}", f"pnkx:{done}"}
    top_mirror = by_ref[f"pnkx:{top}"]
    assert top_mirror.title == "顶层任务"
    assert top_mirror.status == "active"
    assert top_mirror.source == "pnkx"
    assert top_mirror.priority == 3
    assert top_mirror.group_label == "工作"
    assert top_mirror.next_fire_at is None  # 镜像永不触发本地提醒
    done_mirror = by_ref[f"pnkx:{done}"]
    assert done_mirror.status == "done"


async def test_second_sync_is_stable_and_detects_remote_delete(
    database: Database, user_id: UUID
) -> None:
    fake = FakePnkx()
    kept = fake.add_item(content="保留")
    removed = fake.add_item(content="将被远端删除")
    service = _service(database, fake)

    await service.sync_once()
    fake.items.pop(removed)
    fake.items[kept]["updateTime"] = "2026-09-02 09:00:00"

    stats = await service.sync_once()
    assert stats.mirrors_created == 0
    assert stats.mirrors_updated == 1
    assert stats.mirrors_cancelled == 1
    mirrors = {record.title: record for record in await _mirrors(database, user_id)}
    assert mirrors["保留"].status == "active"
    assert mirrors["将被远端删除"].status == "cancelled"


async def test_local_completion_pushes_to_remote(database: Database, user_id: UUID) -> None:
    fake = FakePnkx()
    external_id = fake.add_item(content="要在本地完成的任务")
    service = _service(database, fake)
    await service.sync_once()

    mirrors = await _mirrors(database, user_id)
    mirror = next(record for record in mirrors if record.source_ref == f"pnkx:{external_id}")
    from app.tasks.store import TaskStore

    await TaskStore(database).complete_task(user_id, mirror.id, now=NOW)
    stats = await service.sync_once()

    assert stats.completions_pushed == 1
    assert fake.items[external_id]["status"] is True
    # 再次同步不再重复推送
    stats_again = await service.sync_once()
    assert stats_again.completions_pushed == 0


async def test_manual_task_pushed_once_and_crash_adoption(
    database: Database, user_id: UUID
) -> None:
    from app.tasks.models import TaskKind, TaskTrigger
    from app.tasks.store import TaskStore

    fake = FakePnkx()
    tasks = TaskStore(database)
    local = await tasks.create(
        user_id=user_id,
        kind=TaskKind.TASK,
        title="Aria 手建任务",
        trigger=TaskTrigger(type="time", at=NOW + timedelta(days=1)),
        now=NOW,
    )
    service = _service(database, fake)

    stats = await service.sync_once()
    assert stats.new_pushed == 1
    pushed = fake.created_bodies[0]
    assert pushed["content"] == "Aria 手建任务"
    assert pushed["clientUuid"] == f"aria:{local.id}"
    mirrors = await _mirrors(database, user_id)
    assert any(record.source_ref and record.source_ref.startswith("pnkx:") for record in mirrors)

    # 模拟崩溃后重放：远端已有 aria:{id}，本地回滚 source_ref 再同步 → 认领而非重复创建
    async with database.sessions.begin() as session:
        from sqlalchemy import update

        await session.execute(
            update(TaskItemRecord)
            .where(TaskItemRecord.id == local.id)
            .values(source="manual", source_ref=None)
        )
    fake.created_bodies.clear()
    stats_replay = await service.sync_once()
    assert stats_replay.new_pushed == 0
    assert stats_replay.adopted_after_crash == 1
    assert fake.created_bodies == []


async def test_pull_failure_reported_without_partial_writes(
    database: Database, user_id: UUID
) -> None:
    fake = FakePnkx()
    fake.add_item(content=" unreachable 之前的任务")

    def broken_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/toDo/list":
            return httpx.Response(500, json={"code": 500, "msg": "boom"})
        return fake.handler(request)

    client = PnkxTodoClient(
        base_url="https://pnkx.test",
        integration_token="integration-secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(broken_handler)),
    )
    service = TodoSyncService(database, client, clock=lambda: NOW)
    stats = await service.sync_once()
    await client.close()
    assert stats.pulled == 0
    assert stats.errors and stats.errors[0].startswith("pull_failed")
    assert await _mirrors(database, user_id) == []


# ---------------------------------------------------------------------------
# 调度器与 API
# ---------------------------------------------------------------------------


async def test_sync_scheduler_first_tick_delayed(database: Database, user_id: UUID) -> None:
    import asyncio

    fake = FakePnkx()
    fake.add_item(content="调度器任务")
    service = _service(database, fake)
    scheduler = TodoSyncScheduler(service, interval_seconds=0.05)
    scheduler.start()
    assert scheduler.last_stats is None  # 首个 tick 前不执行
    for _ in range(50):
        if scheduler.last_stats is not None:
            break
        await asyncio.sleep(0.02)
    await scheduler.stop()
    assert scheduler.last_stats is not None
    assert scheduler.last_stats.pulled == 1


async def test_todo_sync_api_requires_auth_and_returns_stats(
    database: Database,
) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="Todo user", password="correct horse")
    fake = FakePnkx()
    fake.add_item(content="API 任务")
    service = _service(database, fake)
    app = FastAPI()
    app.include_router(create_todo_router(service, auth))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.post("/api/v1/todo/sync")
        authorized = await client.post(
            "/api/v1/todo/sync",
            headers={"Authorization": f"Bearer {owner.access_token}"},
        )
    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert authorized.json()["pulled"] == 1
    assert authorized.json()["mirrors_created"] == 1
