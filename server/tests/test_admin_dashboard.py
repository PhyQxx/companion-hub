from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.admin_dashboard import create_admin_dashboard_router
from app.db import (
    AppUserRecord,
    Base,
    ConversationRecord,
    Database,
    MessageRecord,
    create_database,
)
from app.ids import uuid7


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    result = create_database("sqlite+aiosqlite:///:memory:")
    async with result.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield result
    finally:
        await result.close()


@pytest.fixture
def client_app(database: Database) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_admin_dashboard_router(
            database,
            admin_token="test-admin-token",
            version="0.1.0",
        )
    )
    return app


async def _seed_usage_data(database: Database) -> None:
    now = datetime.now(UTC)
    first_conversation = uuid7()
    second_conversation = uuid7()
    async with database.sessions.begin() as session:
        user = AppUserRecord(id=uuid7(), display_name="Usage", status="active")
        session.add(user)
        session.add(
            ConversationRecord(
                id=first_conversation,
                user_id=user.id,
                title="active chat",
                status="active",
                last_seq=2,
                last_turn_seq=2,
                created_at=now,
                last_active_at=now,
            )
        )
        session.add(
            ConversationRecord(
                id=second_conversation,
                user_id=user.id,
                title="archived chat",
                status="archived",
                last_seq=1,
                last_turn_seq=1,
                created_at=now,
                last_active_at=now,
            )
        )
        session.add(
            MessageRecord(
                id=uuid4(),
                conversation_id=first_conversation,
                turn_id=uuid4(),
                seq=1,
                role="user",
                content="你好",
                privacy_level="L1",
                created_at=now,
            )
        )
        session.add(
            MessageRecord(
                id=uuid4(),
                conversation_id=first_conversation,
                turn_id=uuid4(),
                seq=2,
                role="assistant",
                content="你好呀",
                privacy_level="L1",
                created_at=now,
            )
        )
        session.add(
            MessageRecord(
                id=uuid4(),
                conversation_id=second_conversation,
                turn_id=uuid4(),
                seq=1,
                role="user",
                content="记录一条敏感信息",
                privacy_level="L2",
                created_at=now,
            )
        )


class TestUsageDashboard:
    async def test_usage_counts_by_role_and_privacy(
        self, database: Database, client_app: FastAPI
    ) -> None:
        await _seed_usage_data(database)
        headers = {"Authorization": "Bearer test-admin-token"}

        async with AsyncClient(
            transport=ASGITransport(app=client_app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/admin/dashboard/usage", headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert body["total_conversations"] == 2
        assert body["active_conversations"] == 1
        assert body["total_messages"] == 3
        assert body["messages_by_role"] == {"user": 2, "assistant": 1}
        assert body["messages_by_privacy_level"] == {"L1": 2, "L2": 1}

    async def test_usage_empty_database(self, client_app: FastAPI) -> None:
        headers = {"Authorization": "Bearer test-admin-token"}

        async with AsyncClient(
            transport=ASGITransport(app=client_app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/admin/dashboard/usage", headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert body["total_conversations"] == 0
        assert body["active_conversations"] == 0
        assert body["total_messages"] == 0
        assert body["messages_by_role"] == {}
        assert body["messages_by_privacy_level"] == {}

    async def test_usage_requires_admin_token(self, client_app: FastAPI) -> None:
        async with AsyncClient(
            transport=ASGITransport(app=client_app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/admin/dashboard/usage")

        assert response.status_code == 401
