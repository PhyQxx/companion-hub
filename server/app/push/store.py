"""Web Push 订阅存储：按 endpoint upsert、失效即删。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult

from app.db import Database, PushSubscriptionRecord
from app.ids import uuid7


class PushSubscriptionStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def subscribe(
        self,
        *,
        user_id: UUID,
        endpoint: str,
        p256dh: str,
        auth: str,
    ) -> PushSubscriptionRecord:
        """按 endpoint upsert：已存在则重绑用户与密钥（浏览器换账号登录）。"""
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            record = await session.scalar(
                select(PushSubscriptionRecord).where(
                    PushSubscriptionRecord.endpoint == endpoint
                )
            )
            if record is None:
                record = PushSubscriptionRecord(
                    id=uuid7(),
                    user_id=user_id,
                    endpoint=endpoint,
                    p256dh=p256dh,
                    auth=auth,
                    consecutive_failures=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(record)
            record.user_id = user_id
            record.p256dh = p256dh
            record.auth = auth
            record.consecutive_failures = 0
            record.updated_at = now
        refreshed = await self.get_by_endpoint(endpoint)
        assert refreshed is not None
        return refreshed

    async def get_by_endpoint(self, endpoint: str) -> PushSubscriptionRecord | None:
        async with self._database.sessions() as session:
            record = await session.scalar(
                select(PushSubscriptionRecord).where(PushSubscriptionRecord.endpoint == endpoint)
            )
        return record

    async def unsubscribe(self, *, user_id: UUID, endpoint: str) -> bool:
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                delete(PushSubscriptionRecord).where(
                    PushSubscriptionRecord.user_id == user_id,
                    PushSubscriptionRecord.endpoint == endpoint,
                )
            )
        return bool(cast(CursorResult[Any], result).rowcount)

    async def delete(self, subscription_id: UUID) -> None:
        async with self._database.sessions.begin() as session:
            await session.execute(
                delete(PushSubscriptionRecord).where(PushSubscriptionRecord.id == subscription_id)
            )

    async def list_for_user(self, user_id: UUID) -> list[PushSubscriptionRecord]:
        async with self._database.sessions() as session:
            records = (
                await session.scalars(
                    select(PushSubscriptionRecord)
                    .where(PushSubscriptionRecord.user_id == user_id)
                    .order_by(PushSubscriptionRecord.created_at)
                    .limit(200)
                )
            ).all()
        return list(records)

    async def mark_delivered(self, subscription_id: UUID) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            record = await session.get(PushSubscriptionRecord, subscription_id)
            if record is None:
                return
            record.consecutive_failures = 0
            record.last_delivered_at = now
            record.updated_at = now

    async def mark_failed(self, subscription_id: UUID) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            record = await session.get(PushSubscriptionRecord, subscription_id)
            if record is None:
                return
            record.consecutive_failures += 1
            record.last_failed_at = now
            record.updated_at = now
