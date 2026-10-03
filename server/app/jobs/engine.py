from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Database, JobRecord, JobStepRecord, TaskRunRecord
from app.db.claims import lock_job
from app.ids import uuid7
from app.runs.store import transition_run

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
        source_turn_id: UUID | None = None,
    ) -> JobView:
        """提交新 Job。若 idempotency_key 已存在则返回已有 Job。"""
        try:
            async with self._database.sessions.begin() as session:
                return await self.submit_in_session(
                    session,
                    kind,
                    input,
                    owner=owner,
                    priority=priority,
                    idempotency_key=idempotency_key,
                    resource_class=resource_class,
                    max_attempts=max_attempts,
                    available_at=available_at,
                    source_turn_id=source_turn_id,
                )
        except IntegrityError as error:
            if idempotency_key is None:
                raise
            async with self._database.sessions() as session:
                existing = await session.scalar(
                    select(JobRecord).where(
                        JobRecord.idempotency_key == idempotency_key,
                    )
                )
                if existing is None:
                    raise
                if existing.owner != owner or existing.kind != kind or existing.input != input:
                    raise ValueError("job_idempotency_conflict") from error
                return self._to_view(existing)

    async def submit_scheduled(
        self,
        kind: str,
        input: dict[str, Any],
        *,
        schedule_id: str,
        scheduled_slot: datetime,
        owner: str,
        resource_class: str,
    ) -> JobView:
        if scheduled_slot.tzinfo is None:
            raise ValueError("schedule_slot_requires_timezone")
        slot = scheduled_slot.astimezone(UTC).isoformat()
        key = hashlib.sha256(f"{owner}:{schedule_id}:{slot}".encode()).hexdigest()
        return await self.submit(
            kind,
            {**input, "scheduled_slot": slot},
            owner=owner,
            idempotency_key=f"schedule:{key}",
            resource_class=resource_class,
        )

    async def submit_in_session(
        self,
        session: AsyncSession,
        kind: str,
        input: dict[str, Any],
        *,
        owner: str,
        priority: int = 50,
        idempotency_key: str | None = None,
        resource_class: str = "cpu-small",
        max_attempts: int = 3,
        available_at: datetime | None = None,
        task_run_id: UUID | None = None,
        source_turn_id: UUID | None = None,
    ) -> JobView:
        """Enqueue atomically with the caller's domain transaction."""
        if idempotency_key is not None:
            existing = await session.scalar(
                select(JobRecord).where(
                    JobRecord.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if existing.owner != owner or existing.kind != kind or existing.input != input:
                    raise ValueError("job_idempotency_conflict")
                return self._to_view(existing)
        required_run = None
        if kind.startswith("deleg.") and (task_run_id is not None or source_turn_id is not None):
            from app.runs.contracts import lock_source_run

            source_id = task_run_id or source_turn_id
            assert source_id is not None
            required_run = await lock_source_run(session, source_id, user_id=UUID(owner))
        if task_run_id is not None or source_turn_id is not None:
            run = await session.get(TaskRunRecord, task_run_id or source_turn_id)
            if run is not None:
                if str(run.user_id) != owner:
                    raise ValueError("job_task_run_owner_mismatch")
                task_run_id = run.id
            elif task_run_id is not None:
                raise LookupError("task run not found")
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
            available_at=available_at or datetime.now(UTC),
            task_run_id=task_run_id,
        )
        session.add(job)
        await session.flush()
        if required_run is not None:
            from app.runs.contracts import require_work

            await require_work(session, required_run, kind="delegated_job", work_id=job.id)
        return self._to_view(job)

    async def get(self, job_id: UUID) -> JobView | None:
        async with self._database.sessions() as session:
            record = await session.get(JobRecord, job_id)
            return self._to_view(record) if record is not None else None

    async def job_input(self, job_id: UUID) -> dict[str, Any] | None:
        """Worker 侧读取任务输入（JobView 刻意不透出 input）。"""
        async with self._database.sessions() as session:
            record = await session.get(JobRecord, job_id)
            return dict(record.input) if record is not None else None

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

    async def count_jobs(
        self,
        *,
        owner: str | None = None,
        status: JobStatus | None = None,
        kind: str | None = None,
    ) -> int:
        async with self._database.sessions() as session:
            stmt = select(func.count()).select_from(JobRecord)
            if owner is not None:
                stmt = stmt.where(JobRecord.owner == owner)
            if status is not None:
                stmt = stmt.where(JobRecord.status == status)
            if kind is not None:
                stmt = stmt.where(JobRecord.kind == kind)
            return int(await session.scalar(stmt) or 0)

    # ------------------------------------------------------------------ #
    # Worker 领取与租约
    # ------------------------------------------------------------------ #

    async def claim(
        self,
        worker_id: str,
        *,
        resource_class: str = "cpu-small",
        lease_seconds: float = 300.0,
        job_id: UUID | None = None,
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
                    or_(JobRecord.lease_expires_at.is_(None), JobRecord.lease_expires_at <= now),
                    JobRecord.available_at <= now,
                    JobRecord.attempts < JobRecord.max_attempts,
                    JobRecord.resource_class == resource_class,
                    *([JobRecord.id == job_id] if job_id is not None else []),
                    JobRecord.cancel_requested_at.is_(None),
                )
                .order_by(JobRecord.priority.desc(), JobRecord.available_at.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
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

            claimed = await session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == record.id,
                    JobRecord.status == record.status,
                    JobRecord.attempts == record.attempts,
                    JobRecord.cancel_requested_at.is_(None),
                    or_(JobRecord.lease_expires_at.is_(None), JobRecord.lease_expires_at <= now),
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if not int(cast(CursorResult[Any], claimed).rowcount or 0):
                return None
            record.status = values["status"]
            record.lease_owner = values["lease_owner"]
            record.lease_expires_at = values["lease_expires_at"]
            record.started_at = values["started_at"]
            record.attempts = values["attempts"]
            return self._to_view(record)

    async def renew_lease(
        self,
        job_id: UUID,
        worker_id: str,
        *,
        lease_seconds: float = 300.0,
        claim_version: int | None = None,
    ) -> bool:
        """续约：只有当前持有者可以续约。"""
        expires = datetime.now(UTC) + timedelta(seconds=lease_seconds)
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == job_id,
                    JobRecord.lease_owner == worker_id,
                    JobRecord.status.in_({"running", "admitted", "cancelling"}),
                    JobRecord.lease_expires_at > datetime.now(UTC),
                    *([JobRecord.attempts == claim_version] if claim_version is not None else []),
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
        worker_id: str | None = None,
        claim_version: int | None = None,
    ) -> int:
        """开始执行一个 step，返回 step id。"""
        async with self._database.sessions.begin() as session:
            # 获取当前 attempt 数
            job = await session.get(JobRecord, job_id, with_for_update=True)
            if job is None:
                raise LookupError("job not found")
            if job.status not in {"running", "admitted"}:
                raise RuntimeError("job_claim_lost")
            if (worker_id is not None and job.lease_owner != worker_id) or (
                claim_version is not None and job.attempts != claim_version
            ):
                raise RuntimeError("job_claim_lost")
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
                update(JobRecord).where(JobRecord.id == job_id).values(current_step=step_name)
            )
            return step.id

    async def complete_step(
        self,
        step_id: int,
        *,
        progress: float | None = None,
        worker_id: str | None = None,
        claim_version: int | None = None,
    ) -> bool:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            step = await session.get(JobStepRecord, step_id)
            if step is None:
                return False
            job = await session.get(JobRecord, step.job_id, with_for_update=True)
            if job is None or job.status not in {"running", "admitted"}:
                return False
            if (
                job.attempts != step.attempt
                or (worker_id is not None and job.lease_owner != worker_id)
                or (claim_version is not None and job.attempts != claim_version)
            ):
                return False
            step.status = "completed"
            step.completed_at = now
            if progress is not None:
                step.progress = progress
            return True

    async def fail_step(
        self,
        step_id: int,
        *,
        error_code: str,
        error_detail: dict[str, Any] | None = None,
        retryable: bool = True,
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            step = await session.get(JobStepRecord, step_id)
            if step is None:
                raise LookupError("step not found")
            job = await session.get(JobRecord, step.job_id, with_for_update=True)
            if job is None:
                raise LookupError("job not found")

            if job.attempts != step.attempt or job.status not in {"running", "admitted"}:
                return
            step.status = "failed"
            step.completed_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            if not retryable or job.attempts >= job.max_attempts:
                job.status = "failed"
                job.error_code = error_code
                job.error_detail_safe = error_detail
                job.completed_at = now
                if job.task_run_id == job.id:
                    await transition_run(session, job.id, "failed")
            else:
                # 进入重试等待
                job.status = "retry_wait"
                backoff = timedelta(seconds=2**job.attempts)
                job.available_at = now + backoff
                job.error_code = error_code
                job.error_detail_safe = error_detail

    # ------------------------------------------------------------------ #
    # 取消与完成
    # ------------------------------------------------------------------ #

    async def cancel(
        self,
        job_id: UUID,
        *,
        worker_id: str | None = None,
        claim_version: int | None = None,
    ) -> bool:
        """请求取消 Job。"""
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            record = await lock_job(session, job_id)
            if record is None or record.status in _TERMINAL:
                return False
            if (worker_id is not None and record.lease_owner != worker_id) or (
                claim_version is not None and record.attempts != claim_version
            ):
                return False
            record.cancel_requested_at = now
            if record.status in ("running", "admitted"):
                record.status = "cancelling"
            else:
                record.status = "cancelled"
                record.completed_at = now
                if record.task_run_id == record.id:
                    await transition_run(session, record.id, "cancelled")
            return True

    async def cancel_requested(self, job_id: UUID) -> bool:
        """Handler 协作式取消检查：取消已请求且 Job 未到终态。"""
        async with self._database.sessions() as session:
            record = await session.get(JobRecord, job_id)
            if record is None or record.status in _TERMINAL:
                return False
            return record.cancel_requested_at is not None

    async def claim_active(self, job_id: UUID, worker_id: str, claim_version: int) -> bool:
        """A stale or deleted claim must stop before its next external operation."""
        async with self._database.sessions() as session:
            return (
                await session.scalar(
                    select(JobRecord.id).where(
                        JobRecord.id == job_id,
                        JobRecord.lease_owner == worker_id,
                        JobRecord.attempts == claim_version,
                        JobRecord.status.in_({"running", "admitted"}),
                        JobRecord.cancel_requested_at.is_(None),
                        JobRecord.lease_expires_at > datetime.now(UTC),
                    )
                )
                is not None
            )

    async def confirm_cancelled(
        self, job_id: UUID, worker_id: str, *, claim_version: int | None = None
    ) -> bool:
        """Worker 确认取消完成。"""
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == job_id,
                    JobRecord.lease_owner == worker_id,
                    JobRecord.status == "cancelling",
                    *([JobRecord.attempts == claim_version] if claim_version is not None else []),
                )
                .values(
                    status="cancelled", completed_at=now, lease_owner=None, lease_expires_at=None
                )
                .returning(JobRecord.id, JobRecord.task_run_id)
            )
            changed = result.one_or_none()
            if changed is not None and changed[1] == job_id:
                await transition_run(session, job_id, "cancelled")
            return changed is not None

    async def succeed(
        self,
        job_id: UUID,
        *,
        worker_id: str | None = None,
        claim_version: int | None = None,
    ) -> bool:
        """标记 Job 成功完成。"""
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            conditions = []
            if worker_id is not None:
                conditions.extend(
                    [JobRecord.lease_owner == worker_id, JobRecord.lease_expires_at > now]
                )
            if claim_version is not None:
                conditions.append(JobRecord.attempts == claim_version)
            result = await session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == job_id,
                    *conditions,
                    JobRecord.status.in_({"running", "admitted"}),
                )
                .values(
                    status="succeeded",
                    progress=1.0,
                    completed_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                )
                .returning(JobRecord.id, JobRecord.task_run_id)
            )
            changed = result.one_or_none()
            if changed is not None and changed[1] == job_id:
                await transition_run(session, job_id, "succeeded")
            return changed is not None

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
                await session.scalars(select(JobRecord).where(JobRecord.lease_owner == worker_id))
            )
            return [self._to_view(r) for r in records]

    async def release_ready_retries(self, *, resource_class: str) -> int:
        """Release due retries only within the caller's owned resource pool."""
        criteria = (
            JobRecord.resource_class == resource_class,
            JobRecord.status == "retry_wait",
            JobRecord.available_at <= datetime.now(UTC),
            JobRecord.cancel_requested_at.is_(None),
            JobRecord.attempts < JobRecord.max_attempts,
        )
        # An empty UPDATE still takes SQLite's writer lock. Idle maintenance
        # pools must not block source acceptance in unrelated transactions.
        # Close this read before writing, avoiding read-to-write lock upgrades.
        async with self._database.sessions() as session:
            ready = await session.scalar(select(JobRecord.id).where(*criteria).limit(1))
        if ready is None:
            return 0
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                update(JobRecord)
                .where(*criteria)
                .values(status="queued", lease_owner=None, lease_expires_at=None)
            )
            return int(cast(CursorResult[Any], result).rowcount or 0)

    async def expire_stale_leases(self, *, resource_class: str | None = None) -> int:
        """清理过期的 worker 租约，将超时 Job 重置为 queued 或 failed。

        返回被清理的 Job 数量。
        """
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            # 先找出所有过期租约
            stale = list(
                await session.scalars(
                    select(JobRecord)
                    .where(
                        JobRecord.lease_expires_at < now,
                        *([JobRecord.resource_class == resource_class] if resource_class else []),
                        JobRecord.status.in_({"running", "admitted", "cancelling"}),
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            count = 0
            for record in stale:
                if record.status == "cancelling":
                    record.status = "cancelled"
                    record.completed_at = now
                    record.lease_owner = None
                    record.lease_expires_at = None
                elif record.attempts >= record.max_attempts:
                    record.status = "failed"
                    record.error_code = "lease_expired"
                    record.completed_at = now
                    record.lease_owner = None
                    record.lease_expires_at = None
                else:
                    record.status = "queued"
                    record.lease_owner = None
                    record.lease_expires_at = None
                if record.task_run_id == record.id and record.status in {"failed", "cancelled"}:
                    await transition_run(session, record.id, record.status)
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
