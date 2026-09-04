from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult

from app.db import Database, RuntimeLeaseRecord

LeaseType = Literal["audio_output", "microphone"]


@dataclass(frozen=True, slots=True)
class LeaseResult:
    acquired: bool
    lease_type: LeaseType
    holder_device_id: UUID
    epoch: int
    expires_at: datetime
    previous_holder: UUID | None = None
    reason_code: str | None = None


class LeaseManager:
    """运行时租约管理：音频输出和麦克风所有权仲裁。

    每类租约全局唯一（按 lease_type），使用 epoch 防止网络分区双播。
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    async def acquire(
        self,
        lease_type: LeaseType,
        holder_device_id: UUID,
        *,
        generation_id: UUID | None = None,
        ttl_seconds: float = 30.0,
    ) -> LeaseResult:
        """获取租约。若已有持有者，抢占并返回旧持有者信息。"""
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=ttl_seconds)

        async with self._database.sessions.begin() as session:
            existing = await session.scalar(
                select(RuntimeLeaseRecord).where(
                    RuntimeLeaseRecord.lease_type == lease_type
                )
            )
            if existing is None:
                record = RuntimeLeaseRecord(
                    lease_type=lease_type,
                    holder_device_id=holder_device_id,
                    generation_id=generation_id,
                    epoch=1,
                    expires_at=expires,
                )
                session.add(record)
                return LeaseResult(
                    acquired=True,
                    lease_type=lease_type,
                    holder_device_id=holder_device_id,
                    epoch=1,
                    expires_at=expires,
                )

            prev_holder = existing.holder_device_id
            new_epoch = existing.epoch + 1
            existing.holder_device_id = holder_device_id
            existing.generation_id = generation_id
            existing.epoch = new_epoch
            existing.expires_at = expires
            return LeaseResult(
                acquired=True,
                lease_type=lease_type,
                holder_device_id=holder_device_id,
                epoch=new_epoch,
                expires_at=expires,
                previous_holder=prev_holder,
            )

    async def renew(
        self,
        lease_type: LeaseType,
        holder_device_id: UUID,
        *,
        ttl_seconds: float = 30.0,
    ) -> LeaseResult:
        """续约：只有当前持有者可以续约。"""
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=ttl_seconds)

        async with self._database.sessions.begin() as session:
            result = await session.execute(
                update(RuntimeLeaseRecord)
                .where(
                    RuntimeLeaseRecord.lease_type == lease_type,
                    RuntimeLeaseRecord.holder_device_id == holder_device_id,
                )
                .values(expires_at=expires)
            )
            if int(cast(CursorResult[Any], result).rowcount or 0) == 0:
                return LeaseResult(
                    acquired=False,
                    lease_type=lease_type,
                    holder_device_id=holder_device_id,
                    epoch=0,
                    expires_at=now,
                    reason_code="not_holder",
                )
            record = await session.scalar(
                select(RuntimeLeaseRecord).where(
                    RuntimeLeaseRecord.lease_type == lease_type
                )
            )
            assert record is not None
            return LeaseResult(
                acquired=True,
                lease_type=lease_type,
                holder_device_id=holder_device_id,
                epoch=record.epoch,
                expires_at=expires,
            )

    async def release(
        self,
        lease_type: LeaseType,
        holder_device_id: UUID,
    ) -> bool:
        """释放租约。非持有者释放返回 False（不报错）。"""
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                delete(RuntimeLeaseRecord).where(
                    RuntimeLeaseRecord.lease_type == lease_type,
                    RuntimeLeaseRecord.holder_device_id == holder_device_id,
                )
            )
            return int(cast(CursorResult[Any], result).rowcount or 0) > 0

    async def release_for_generation(
        self,
        lease_type: LeaseType,
        generation_id: UUID,
    ) -> bool:
        """按回合释放租约：中断路径只知道 generation，不知道持有者是谁。"""
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                delete(RuntimeLeaseRecord).where(
                    RuntimeLeaseRecord.lease_type == lease_type,
                    RuntimeLeaseRecord.generation_id == generation_id,
                )
            )
            return int(cast(CursorResult[Any], result).rowcount or 0) > 0

    async def current_holder(
        self, lease_type: LeaseType
    ) -> LeaseResult | None:
        """查询当前持有者，不检查过期时间（调用方应自行处理过期）。"""
        async with self._database.sessions() as session:
            record = await session.scalar(
                select(RuntimeLeaseRecord).where(
                    RuntimeLeaseRecord.lease_type == lease_type
                )
            )
            if record is None:
                return None
            return LeaseResult(
                acquired=True,
                lease_type=lease_type,
                holder_device_id=record.holder_device_id,
                epoch=record.epoch,
                expires_at=record.expires_at,
            )

    async def expire_stale(self) -> list[LeaseResult]:
        """清理已过期的租约，返回被清理的列表。"""
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            stale = await session.scalars(
                select(RuntimeLeaseRecord).where(
                    RuntimeLeaseRecord.expires_at < now
                )
            )
            results = [
                LeaseResult(
                    acquired=False,
                    lease_type=r.lease_type,  # type: ignore[arg-type]
                    holder_device_id=r.holder_device_id,
                    epoch=r.epoch,
                    expires_at=r.expires_at,
                    reason_code="expired",
                )
                for r in stale.all()
            ]
            await session.execute(
                delete(RuntimeLeaseRecord).where(
                    RuntimeLeaseRecord.expires_at < now
                )
            )
            return results
