"""Short-lived, user-bound previews for mutations requiring an explicit UI click.

两种实现共享同一 async 契约：
- ``PendingMutationStore``：进程内存实现，测试与无数据库环境兜底；
- ``DatabasePendingMutationStore``：数据库实现，预览跨重启/跨 worker 存活，
  认领走 ``status='pending'`` 条件更新的原子守卫。

安全语义不变：预览绑定用户与回合、15 分钟有效、同回合修改作废旧预览、
发送/保存结果未知时置 ``unknown_outcome`` 且绝不自动重试。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import CursorResult

from app.db import Database, PendingMutationRecord

PREVIEW_TTL_MINUTES = 15


def _digest_of(content: dict[str, Any]) -> str:
    return sha256(
        json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass
class PendingMutation:
    id: UUID
    user_id: UUID
    turn_id: UUID
    kind: str
    content: dict[str, Any]
    preview: dict[str, Any]
    digest: str
    expires_at: datetime
    status: str = "pending"
    result: dict[str, Any] | None = None

    def view(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "kind": self.kind,
            "preview": self.preview,
            "digest": self.digest,
            "expires_at": self.expires_at.isoformat(),
            "status": self.status,
            "result": self.result,
        }


def _row_to_item(record: PendingMutationRecord) -> PendingMutation:
    expires_at = record.expires_at
    if expires_at.tzinfo is None:  # SQLite 返回 naive
        expires_at = expires_at.replace(tzinfo=UTC)
    return PendingMutation(
        id=record.id,
        user_id=record.user_id,
        turn_id=record.turn_id,
        kind=record.kind,
        content=dict(record.content),
        preview=dict(record.preview),
        digest=record.digest,
        expires_at=expires_at,
        status=record.status,
        result=dict(record.result) if record.result is not None else None,
    )


class PendingMutationStore:
    """进程内存预览存储；无 await 的认领在单进程内阻止并发重复确认。"""

    def __init__(
        self, *, limit: int = 256, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._limit = limit
        self._clock = clock or (lambda: datetime.now(UTC))
        self._items: dict[UUID, PendingMutation] = {}

    async def prepare(
        self,
        *,
        user_id: UUID,
        turn_id: UUID,
        kind: str,
        content: dict[str, Any],
        preview: dict[str, Any],
        now: datetime | None = None,
    ) -> PendingMutation:
        current = now or self._clock()
        self._prune(current)
        digest = _digest_of(content)
        for item in self._items.values():
            if item.user_id == user_id and item.turn_id == turn_id and item.kind == kind:
                if item.digest == digest:
                    return item
                if item.status == "pending":
                    item.status = "cancelled"
        if len(self._items) >= self._limit:
            raise OverflowError("confirmation_preview_capacity")
        item = PendingMutation(
            id=uuid4(),
            user_id=user_id,
            turn_id=turn_id,
            kind=kind,
            content=content,
            preview=preview,
            digest=digest,
            expires_at=current + timedelta(minutes=PREVIEW_TTL_MINUTES),
        )
        self._items[item.id] = item
        return item

    async def list(self, user_id: UUID, *, now: datetime | None = None) -> list[PendingMutation]:
        current = now or self._clock()
        self._prune(current)
        return [item for item in self._items.values() if item.user_id == user_id]

    async def claim(self, user_id: UUID, item_id: UUID, digest: str) -> PendingMutation:
        item = self._owned(user_id, item_id)
        if digest != item.digest:
            raise ValueError("confirmation_preview_changed")
        if item.status == "completed":
            return item
        if item.status != "pending":
            raise ValueError("confirmation_preview_not_pending")
        item.status = "saving"
        return item

    async def complete(self, item: PendingMutation, result: dict[str, Any]) -> PendingMutation:
        item.result = result
        item.status = "completed"
        return item

    async def mark_unknown(self, item: PendingMutation) -> None:
        item.status = "unknown_outcome"

    async def cancel(self, user_id: UUID, item_id: UUID) -> PendingMutation:
        item = self._owned(user_id, item_id)
        if item.status != "pending":
            raise ValueError("confirmation_preview_not_pending")
        item.status = "cancelled"
        return item

    def _owned(self, user_id: UUID, item_id: UUID) -> PendingMutation:
        item = self._items.get(item_id)
        if item is None or item.user_id != user_id:
            raise LookupError("confirmation_preview_not_found")
        if item.expires_at <= self._clock():
            raise ValueError("confirmation_preview_expired")
        return item

    def _prune(self, now: datetime) -> None:
        self._items = {
            key: item
            for key, item in self._items.items()
            if item.expires_at > now or item.status == "saving"
        }


class DatabasePendingMutationStore:
    """数据库预览存储：跨重启/跨 worker 存活，认领用条件更新做原子守卫。"""

    def __init__(self, database: Database, *, limit: int = 256) -> None:
        self._database = database
        self._limit = limit

    async def prepare(
        self,
        *,
        user_id: UUID,
        turn_id: UUID,
        kind: str,
        content: dict[str, Any],
        preview: dict[str, Any],
        now: datetime | None = None,
    ) -> PendingMutation:
        current = now or datetime.now(UTC)
        await self._prune(current)
        digest = _digest_of(content)
        async with self._database.sessions.begin() as session:
            rows = (
                (
                    await session.execute(
                        select(PendingMutationRecord)
                        .where(
                            PendingMutationRecord.user_id == user_id,
                            PendingMutationRecord.turn_id == turn_id,
                            PendingMutationRecord.kind == kind,
                        )
                        .order_by(PendingMutationRecord.created_at)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                if row.digest == digest:
                    return _row_to_item(row)
            await session.execute(
                update(PendingMutationRecord)
                .where(
                    PendingMutationRecord.user_id == user_id,
                    PendingMutationRecord.turn_id == turn_id,
                    PendingMutationRecord.kind == kind,
                    PendingMutationRecord.status == "pending",
                    PendingMutationRecord.digest != digest,
                )
                .values(status="cancelled")
            )
            count = int(
                await session.scalar(select(func.count()).select_from(PendingMutationRecord)) or 0
            )
            if count >= self._limit:
                raise OverflowError("confirmation_preview_capacity")
            record = PendingMutationRecord(
                id=uuid4(),
                user_id=user_id,
                turn_id=turn_id,
                kind=kind,
                content=content,
                preview=preview,
                digest=digest,
                status="pending",
                expires_at=current + timedelta(minutes=PREVIEW_TTL_MINUTES),
                created_at=current,
                updated_at=current,
            )
            session.add(record)
        return _row_to_item(record)

    async def list(self, user_id: UUID, *, now: datetime | None = None) -> list[PendingMutation]:
        await self._prune(now or datetime.now(UTC))
        async with self._database.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(PendingMutationRecord)
                        .where(PendingMutationRecord.user_id == user_id)
                        .order_by(PendingMutationRecord.created_at)
                    )
                )
                .scalars()
                .all()
            )
        return [_row_to_item(row) for row in rows]

    async def claim(self, user_id: UUID, item_id: UUID, digest: str) -> PendingMutation:
        item = await self._owned(user_id, item_id)
        if digest != item.digest:
            raise ValueError("confirmation_preview_changed")
        if item.status == "completed":
            return item
        if item.status != "pending":
            raise ValueError("confirmation_preview_not_pending")
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                update(PendingMutationRecord)
                .where(
                    PendingMutationRecord.id == item_id,
                    PendingMutationRecord.user_id == user_id,
                    PendingMutationRecord.status == "pending",
                )
                .values(status="saving", updated_at=now)
            )
            claimed = int(cast(CursorResult[Any], result).rowcount or 0)
        if claimed == 0:
            # 并发方已认领：完成方重放结果，其余状态拒绝。
            refreshed = await self._owned(user_id, item_id)
            if refreshed.status == "completed":
                return refreshed
            raise ValueError("confirmation_preview_not_pending")
        item.status = "saving"
        return item

    async def complete(self, item: PendingMutation, result: dict[str, Any]) -> PendingMutation:
        async with self._database.sessions.begin() as session:
            await session.execute(
                update(PendingMutationRecord)
                .where(
                    PendingMutationRecord.id == item.id,
                    PendingMutationRecord.status == "saving",
                )
                .values(
                    status="completed",
                    result=result,
                    updated_at=datetime.now(UTC),
                )
            )
        item.result = result
        item.status = "completed"
        return item

    async def mark_unknown(self, item: PendingMutation) -> None:
        async with self._database.sessions.begin() as session:
            await session.execute(
                update(PendingMutationRecord)
                .where(PendingMutationRecord.id == item.id)
                .values(status="unknown_outcome", updated_at=datetime.now(UTC))
            )
        item.status = "unknown_outcome"

    async def cancel(self, user_id: UUID, item_id: UUID) -> PendingMutation:
        item = await self._owned(user_id, item_id)
        if item.status != "pending":
            raise ValueError("confirmation_preview_not_pending")
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                update(PendingMutationRecord)
                .where(
                    PendingMutationRecord.id == item_id,
                    PendingMutationRecord.status == "pending",
                )
                .values(status="cancelled", updated_at=datetime.now(UTC))
            )
            if int(cast(CursorResult[Any], result).rowcount or 0) == 0:
                raise ValueError("confirmation_preview_not_pending")
        item.status = "cancelled"
        return item

    async def _owned(self, user_id: UUID, item_id: UUID) -> PendingMutation:
        async with self._database.sessions() as session:
            record = await session.scalar(
                select(PendingMutationRecord).where(PendingMutationRecord.id == item_id)
            )
        if record is None or record.user_id != user_id:
            raise LookupError("confirmation_preview_not_found")
        item = _row_to_item(record)
        if item.expires_at <= datetime.now(UTC):
            raise ValueError("confirmation_preview_expired")
        return item

    async def _prune(self, now: datetime) -> None:
        async with self._database.sessions.begin() as session:
            await session.execute(
                delete(PendingMutationRecord).where(
                    PendingMutationRecord.expires_at <= now,
                    PendingMutationRecord.status != "saving",
                )
            )


__all__ = [
    "DatabasePendingMutationStore",
    "PendingMutation",
    "PendingMutationStore",
]
