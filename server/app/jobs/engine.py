from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult

from app.db import Database, JobRecord, JobStepRecord
from app.ids import uuid7

logger = logging.getLogger("app.jobs.engine")

JobStatus = Literal[
    "queued",
    "admitted",
    "running",
    "waiting_user",
    "retry_wait",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
]

_TERMINAL: set[JobStatus] = {"succeeded", "failed", "cancelled"}

# 合法状态转移 (source -> allowed targets)
_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    "queued": {"admitted", "cancelled"},
    "admitted": {"running", "queued", "cancelled"},
    "running": {"waiting_user", "retry_wait", "succeeded", "failed", "cancelling"},
    "waiting_user": {"queued", "cancelled"},
    "retry_wait": {"queued", "cancelled"},
    "cancelling": {"cancelled"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}


@dataclass(frozen=True, slots=True)
class JobView:
    id: UUID
    kind: str
    owner: str
    status: JobStatus
    priority: int
    progress: float
    current_step: str | None
    resource_class: str
    attempts: int
    max_attempts: int
    error_code: str | None
    lease_owner: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class JobEngine:
    """Job 调度引擎。

    负责 Job 的提交、worker 领取、step 执行、取消和状态转移。
    v1 不支持分布式 worker 心跳和 GPU 资源准入控制，预留接口。
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    # ------------------------------------------------------------------ #
    # 提交与查询
    # ------------------------------------------------------------------ #

    async def submit(
        self,
        kind: str,
        input: dict[str, Any],
        *,
        owner: str = "local-user",
        priority: int = 50,
        idempotency_key: str | None = None,
        resource_class: str = "cpu-small",
        max_attempts: int = 3,
        available_at: datetime | None = None,
    ) -> JobView:
        """提交新 Job。若 idempotency_key 已存在则返回已有 Job。"""
        now = datetime.now(UTC)
        if available_at is None:
            available_at = now

        async with self._database.sessions.begin() as session:
            if idempotency_key is not None:
                existing = await session.scalar(
                    select(JobRecord).where(JobRecord.idempotency_key == idempotency_key)
                )
                if existing is not None:
                    return self._to_view(existing)

            job = JobRecord(
                id=uuid7(),
                kind=kind,
                owner=owner,
                status="queued",
                priority=priority,
                idempotency_key=idempotency_key,
                input=input,
                progress=0.0,
                resource_class=resource_class,
                attempts=0,
                max_attempts=max_attempts,
                available_at=available_at,
            )
            session.add(job)
            return self._to_view(job)

    async def get(self, job_id: UUID) -> JobView | None:
        async with self._database.sessions() as session:
            record = await session.get(JobRecord, job_id)
            return self._to_view(record) if record is not None else None

    async def list_jobs(
        self,
        *,
        owner: str | None = None,
        status: JobStatus | None = None,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[JobView]:
        async with self._database.sessions() as session:
            stmt = (
                select(JobRecord).order_by(JobRecord.created_at.desc()).limit(limit).offset(offset)
            )
            if owner is not None:
                stmt = stmt.where(JobRecord.owner == owner)
            if status is not None:
                stmt = stmt.where(JobRecord.status == status)
            if kind is not None:
                stmt = stmt.where(JobRecord.kind == kind)
            records = list(await session.scalars(stmt))
            return [self._to_view(r) for r in records]

    # ------------------------------------------------------------------ #
    # Worker 领取与租约
    # ------------------------------------------------------------------ #

    async def claim(
        self,
        worker_id: str,
        *,
        resource_class: str = "cpu-small",
        lease_seconds: float = 300.0,
    ) -> JobView | None:
        """Worker 领取一个可执行的 Job。

        按优先级最高、available_at 最早排序。
        领取后状态变为 running，并记录租约。
        """
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=lease_seconds)

        async with self._database.sessions.begin() as session:
            # 找可领取的 Job：queued 或 admitted（租约过期）
            record = await session.scalar(
                select(JobRecord)
                .where(
                    JobRecord.status.in_({"queued", "admitted"}),
                    JobRecord.available_at <= now,
                    JobRecord.resource_class == resource_class,
                )
                .order_by(JobRecord.priority.desc(), JobRecord.available_at.asc())
                .limit(1)
            )
            if record is None:
                return None

            values: dict[str, Any] = {
                "status": "running",
                "lease_owner": worker_id,
                "lease_expires_at": expires,
                "started_at": now if record.started_at is None else record.started_at,
                "attempts": record.attempts + 1,
            }
            if record.status == "queued":
                values["status"] = "admitted"

            await session.execute(
                update(JobRecord)
                .where(JobRecord.id == record.id, JobRecord.status == record.status)
                .values(**values)
            )
            record.status = values["status"]
            record.lease_owner = values["lease_owner"]
            record.lease_expires_at = values["lease_expires_at"]
            record.started_at = values["started_at"]
            record.attempts = values["attempts"]
            return self._to_view(record)

    async def renew_lease(
        self, job_id: UUID, worker_id: str, *, lease_seconds: float = 300.0
    ) -> bool:
        """续约：只有当前持有者可以续约。"""
        expires = datetime.now(UTC) + timedelta(seconds=lease_seconds)
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == job_id,
                    JobRecord.lease_owner == worker_id,
                )
                .values(lease_expires_at=expires)
            )
            return int(cast(CursorResult[Any], result).rowcount or 0) > 0

    async def release_lease(self, job_id: UUID, worker_id: str) -> bool:
        """释放租约：Job 回到 queued 状态。"""
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == job_id,
                    JobRecord.lease_owner == worker_id,
                    JobRecord.status == "admitted",
                )
                .values(status="queued", lease_owner=None, lease_expires_at=None)
            )
            return int(cast(CursorResult[Any], result).rowcount or 0) > 0

    # ------------------------------------------------------------------ #
    # Step 生命周期
    # ------------------------------------------------------------------ #

    async def start_step(
        self,
        job_id: UUID,
        step_name: str,
        *,
        checkpoint: dict[str, Any] | None = None,
    ) -> int:
        """开始执行一个 step，返回 step id。"""
        async with self._database.sessions.begin() as session:
            # 获取当前 attempt 数
            job = await session.get(JobRecord, job_id)
            if job is None:
                raise LookupError("job not found")
            attempt = job.attempts
            step = JobStepRecord(
                job_id=job_id,
                name=step_name,
                attempt=attempt,
                status="running",
                checkpoint=checkpoint,
                started_at=datetime.now(UTC),
            )
            session.add(step)
            await session.flush()
            await session.execute(
                update(JobRecord)
                .where(JobRecord.id == job_id)
                .values(current_step=step_name)
            )
            return step.id

    async def complete_step(
        self,
        step_id: int,
        *,
        progress: float | None = None,
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            values: dict[str, Any] = {"status": "completed", "completed_at": now}
            if progress is not None:
                values["progress"] = progress
            await session.execute(
                update(JobStepRecord).where(JobStepRecord.id == step_id).values(**values)
            )

    async def fail_step(
        self,
        step_id: int,
        *,
        error_code: str,
        error_detail: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            step = await session.get(JobStepRecord, step_id)
            if step is None:
                raise LookupError("step not found")
            step.status = "failed"
            step.completed_at = now

            job = await session.get(JobRecord, step.job_id)
            if job is None:
                raise LookupError("job not found")

            if job.attempts >= job.max_attempts:
                job.status = "failed"
                job.error_code = error_code
                job.error_detail_safe = error_detail
                job.completed_at = now
            else:
                # 进入重试等待
                job.status = "retry_wait"
                backoff = timedelta(seconds=2 ** job.attempts)
                job.available_at = now + backoff
                job.error_code = error_code
                job.error_detail_safe = error_detail

    # ------------------------------------------------------------------ #
    # 取消与完成
    # ------------------------------------------------------------------ #

    async def cancel(self, job_id: UUID) -> bool:
        """请求取消 Job。"""
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            record = await session.get(JobRecord, job_id)
            if record is None or record.status in _TERMINAL:
                return False
            record.cancel_requested_at = now
            if record.status in ("running", "admitted"):
                record.status = "cancelling"
            else:
                record.status = "cancelled"
                record.completed_at = now
            return True

    async def confirm_cancelled(self, job_id: UUID, worker_id: str) -> bool:
        """Worker 确认取消完成。"""
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == job_id,
                    JobRecord.lease_owner == worker_id,
                    JobRecord.status == "cancelling",
                )
                .values(
                    status="cancelled", completed_at=now, lease_owner=None, lease_expires_at=None
                )
            )
            return int(cast(CursorResult[Any], result).rowcount or 0) > 0

    async def succeed(self, job_id: UUID) -> bool:
        """标记 Job 成功完成。"""
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == job_id,
                    JobRecord.status.in_({"running", "admitted"}),
                )
                .values(
                    status="succeeded",
                    progress=1.0,
                    completed_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
            return int(cast(CursorResult[Any], result).rowcount or 0) > 0

    # ------------------------------------------------------------------ #
    # Worker 心跳与租约清理
    # ------------------------------------------------------------------ #

    async def heartbeat(self, worker_id: str) -> list[JobView]:
        """Worker 心跳：续约所有持有的租约，返回持有的 Job 列表。"""
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=300)
        async with self._database.sessions.begin() as session:
            await session.execute(
                update(JobRecord)
                .where(
                    JobRecord.lease_owner == worker_id,
                    JobRecord.status.in_({"running", "admitted", "cancelling"}),
                )
                .values(lease_expires_at=expires)
            )
            records = list(
                await session.scalars(
                    select(JobRecord).where(JobRecord.lease_owner == worker_id)
                )
            )
            return [self._to_view(r) for r in records]

    async def expire_stale_leases(self) -> int:
        """清理过期的 worker 租约，将超时 Job 重置为 queued 或 failed。

        返回被清理的 Job 数量。
        """
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            # 先找出所有过期租约
            stale = list(
                await session.scalars(
                    select(JobRecord).where(
                        JobRecord.lease_expires_at < now,
                        JobRecord.status.in_({"running", "admitted", "cancelling"}),
                    )
                )
            )
            count = 0
            for record in stale:
                if record.attempts >= record.max_attempts:
                    record.status = "failed"
                    record.error_code = "lease_expired"
                    record.completed_at = now
                else:
                    record.status = "queued"
                    record.lease_owner = None
                    record.lease_expires_at = None
                count += 1
            return count

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_view(record: JobRecord) -> JobView:
        return JobView(
            id=record.id,
            kind=record.kind,
            owner=record.owner,
            status=record.status,  # type: ignore[arg-type]
            priority=record.priority,
            progress=record.progress,
            current_step=record.current_step,
            resource_class=record.resource_class,
            attempts=record.attempts,
            max_attempts=record.max_attempts,
            error_code=record.error_code,
            lease_owner=record.lease_owner,
            created_at=record.created_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
        )
