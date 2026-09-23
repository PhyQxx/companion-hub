"""FR-S3 数据导出/导入：伴侣数据 JSON 档案的往返、空库守卫与鉴权。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.admin_export import create_admin_export_router
from app.db import (
    AppUserRecord,
    Base,
    ConversationRecord,
    Database,
    MemoryRecord,
    MessageRecord,
    TaskItemRecord,
    create_database,
)
from app.ids import uuid7

AUTH = {"Authorization": "Bearer test-admin-token"}


@pytest.fixture
async def source_db(tmp_path: Any) -> AsyncIterator[Database]:
    value = create_database(f"sqlite+aiosqlite:///{tmp_path / 'source.db'}")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield value
    finally:
        await value.close()


@pytest.fixture
async def target_db(tmp_path: Any) -> AsyncIterator[Database]:
    value = create_database(f"sqlite+aiosqlite:///{tmp_path / 'target.db'}")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield value
    finally:
        await value.close()


def _app(database: Database) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_admin_export_router(
            database=database,
            admin_token="test-admin-token",
            hub_version="test-1.0",
        )
    )
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed(database: Database) -> UUID:
    """用户 + 一段对话两条消息 + 一条记忆 + 一条任务，覆盖核心外键链。"""
    user_id = uuid7()
    conversation_id = uuid7()
    now = datetime.now(UTC)
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=user_id, display_name="搬家用户", status="active"))
        session.add(
            ConversationRecord(
                id=conversation_id,
                user_id=user_id,
                title="日常闲聊",
                created_at=now,
                last_active_at=now,
            )
        )
        session.add(
            MessageRecord(
                id=uuid7(),
                conversation_id=conversation_id,
                turn_id=uuid7(),
                seq=1,
                role="user",
                content="记一下：周五体检",
                privacy_level="L1",
                created_at=now,
            )
        )
        session.add(
            MessageRecord(
                id=uuid7(),
                conversation_id=conversation_id,
                turn_id=uuid7(),
                seq=2,
                role="assistant",
                content="好，已经记住了。",
                privacy_level="L1",
                created_at=now,
            )
        )
        session.add(
            MemoryRecord(
                user_id=user_id,
                subject_kind="user",
                subject_key="user",
                origin_kind="user_statement",
                type="commitment",
                content="用户周五要体检",
                privacy_level="L2",
                importance=0.7,
                created_by="system",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            TaskItemRecord(
                id=uuid7(),
                user_id=user_id,
                title="周五体检",
                kind="reminder",
                status="active",
                trigger_type="time",
                trigger_config={"type": "time", "at": "2026-09-25T08:00:00+08:00"},
                created_at=now,
                updated_at=now,
            )
        )
    return user_id


async def test_export_requires_admin_token(source_db: Database) -> None:
    async with _client(_app(source_db)) as client:
        response = await client.get("/api/v1/admin/data/export")
    assert response.status_code == 401


async def test_export_contains_companion_data_only(source_db: Database, tmp_path: Any) -> None:
    user_id = await _seed(source_db)
    async with _client(_app(source_db)) as client:
        response = await client.get("/api/v1/admin/data/export", headers=AUTH)
    assert response.status_code == 200
    archive = response.json()
    assert archive["format"] == "aria-export"
    assert archive["hub_version"] == "test-1.0"
    tables = archive["tables"]
    # 核心伴侣数据在场
    assert len(tables["app_user"]) == 1
    assert tables["app_user"][0]["id"] == str(user_id)
    assert len(tables["message"]) == 2
    assert len(tables["memory"]) == 1
    assert tables["memory"][0]["content"] == "用户周五要体检"
    assert len(tables["task_item"]) == 1
    # 凭据与机器状态绝不出现
    for forbidden in (
        "auth_credential",
        "auth_session",
        "device_client",
        "device_command",
        "push_subscription",
        "config_version",
        "calendar_oauth_token",
    ):
        assert forbidden not in tables


async def test_import_round_trips_into_empty_database(
    source_db: Database, target_db: Database
) -> None:
    await _seed(source_db)
    async with _client(_app(source_db)) as client:
        archive = (await client.get("/api/v1/admin/data/export", headers=AUTH)).json()

    async with _client(_app(target_db)) as client:
        imported = await client.post(
            "/api/v1/admin/data/import",
            headers=AUTH,
            json={"archive": archive, "confirm": True},
        )
    assert imported.status_code == 200
    summary = imported.json()
    assert summary["inserted"]["message"] == 2
    assert summary["inserted"]["memory"] == 1

    async with target_db.sessions() as session:
        messages = (await session.scalars(select(MessageRecord))).all()
        assert [message.seq for message in messages] == [1, 2]
        conversation_ids = {message.conversation_id for message in messages}
        assert len(conversation_ids) == 1
        memory = (await session.scalars(select(MemoryRecord))).all()
        assert memory[0].content == "用户周五要体检"
        tasks = (await session.scalars(select(TaskItemRecord))).all()
        assert tasks[0].title == "周五体检"
        users = (await session.scalars(select(AppUserRecord))).all()
        assert users[0].display_name == "搬家用户"


async def test_import_refuses_non_empty_target(
    source_db: Database, target_db: Database
) -> None:
    await _seed(source_db)
    await _seed(target_db)
    async with _client(_app(source_db)) as client:
        archive = (await client.get("/api/v1/admin/data/export", headers=AUTH)).json()
    async with _client(_app(target_db)) as client:
        response = await client.post(
            "/api/v1/admin/data/import",
            headers=AUTH,
            json={"archive": archive, "confirm": True},
        )
    assert response.status_code == 409
    assert "空库" in response.json()["detail"]


async def test_import_requires_explicit_confirmation(
    source_db: Database, target_db: Database
) -> None:
    await _seed(source_db)
    async with _client(_app(source_db)) as client:
        archive = (await client.get("/api/v1/admin/data/export", headers=AUTH)).json()
    async with _client(_app(target_db)) as client:
        response = await client.post(
            "/api/v1/admin/data/import",
            headers=AUTH,
            json={"archive": archive, "confirm": False},
        )
    assert response.status_code == 400


async def test_import_rejects_unknown_schema_version(
    source_db: Database, target_db: Database
) -> None:
    await _seed(source_db)
    async with _client(_app(source_db)) as client:
        archive = (await client.get("/api/v1/admin/data/export", headers=AUTH)).json()
    archive["schema_version"] = 99
    async with _client(_app(target_db)) as client:
        response = await client.post(
            "/api/v1/admin/data/import",
            headers=AUTH,
            json={"archive": archive, "confirm": True},
        )
    assert response.status_code == 422
