"""TASK-01 任务存储：CRUD、稍后/完成/取消与 exactly-once 触发认领。

认领语义：候选行先 SELECT，再用 `status='active' AND fire_count=<读到的值>`
做乐观 UPDATE；rowcount=0 视为并发方已认领而跳过。一次性任务认领后进入
firing，投递结束由调用方 finish；进程中断遗留的 firing 由 recover 处理，
宁可少投一次也不重复投递。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult

from app.db import Database, TaskItemRecord
from app.ids import uuid7
from app.schemas.common import PrivacyLevel

from .models import (
    ClaimedTask,
    TaskKind,
    TaskStatus,
    TaskTrigger,
    TaskView,
    compute_next_fire,
    validate_trigger,
)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _to_view(record: TaskItemRecord) -> TaskView:
    return TaskView(
        id=record.id,
        user_id=record.user_id,
        kind=TaskKind(record.kind),
        title=record.title,
        notes=record.notes,
        status=TaskStatus(record.status),
        trigger=TaskTrigger.model_validate(record.trigger_config),
        next_fire_at=_aware(record.next_fire_at) if record.next_fire_at is not None else None,
        last_fired_at=_aware(record.last_fired_at) if record.last_fired_at is not None else None,
        fire_count=record.fire_count,
        last_delivery=record.last_delivery,
        privacy_level=PrivacyLevel(record.privacy_level),
        source=record.source,
        source_ref=record.source_ref,
        priority=record.priority,
        group_label=record.group_label,
        completed_at=_aware(record.completed_at) if record.completed_at is not None else None,
        cancelled_at=_aware(record.cancelled_at) if record.cancelled_at is not None else None,
        created_at=_aware(record.created_at) if record.created_at is not None else None,
        updated_at=_aware(record.updated_at) if record.updated_at is not None else None,
    )


class TaskStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        return self._database

    async def create(
        self,
        *,
        user_id: UUID,
        kind: TaskKind,
        title: str,
        trigger: TaskTrigger,
        notes: str | None = None,
        privacy_level: PrivacyLevel = PrivacyLevel.L1,
        source: str = "manual",
        source_ref: str | None = None,
        now: datetime | None = None,
    ) -> TaskView:
        moment = now or datetime.now(UTC)
        normalized = validate_trigger(trigger, now=moment)
        record = TaskItemRecord(
            id=uuid7(),
            user_id=user_id,
            kind=str(kind),
            title=title,
            notes=notes,
            status=str(TaskStatus.ACTIVE),
            trigger_type=str(normalized.type),
            trigger_config=normalized.model_dump(mode="json"),
            event_type=normalized.event_type,
            source_ref=source_ref,
            next_fire_at=normalized.at if normalized.type == "time" else None,
            fire_count=0,
            privacy_level=str(privacy_level),
            source=source,
            created_at=moment,
            updated_at=moment,
        )
        async with self._database.sessions.begin() as session:
            session.add(record)
        return _to_view(record)

    async def cancel_tasks_by_source_ref(self, user_id: UUID, source_ref: str) -> int:
        """联动取消：外部实体（如日历事件）取消/改期时撤下其提醒任务。"""
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                update(TaskItemRecord)
                .where(
                    TaskItemRecord.user_id == user_id,
                    TaskItemRecord.source_ref == source_ref,
                    TaskItemRecord.status == str(TaskStatus.ACTIVE),
                )
                .values(
                    status=str(TaskStatus.CANCELLED),
                    cancelled_at=now,
                    next_fire_at=None,
                    updated_at=now,
                )
            )
        return int(cast(CursorResult[Any], result).rowcount or 0)

    async def list_tasks(
        self,
        user_id: UUID,
        *,
        status: TaskStatus | None = None,
        limit: int = 200,
    ) -> list[TaskView]:
        query = select(TaskItemRecord).where(TaskItemRecord.user_id == user_id)
        if status is not None:
            query = query.where(TaskItemRecord.status == str(status))
        query = query.order_by(TaskItemRecord.created_at.desc()).limit(limit)
        async with self._database.sessions() as session:
            records = (await session.execute(query)).scalars().all()
        return [_to_view(record) for record in records]

    def _admin_filters(
        self,
        *,
        user_id: UUID | None,
        status: TaskStatus | None,
        kind: TaskKind | None,
        source: str | None,
        query: str | None,
    ) -> list[Any]:
        filters: list[Any] = []
        if user_id is not None:
            filters.append(TaskItemRecord.user_id == user_id)
        if status is not None:
            filters.append(TaskItemRecord.status == str(status))
        if kind is not None:
            filters.append(TaskItemRecord.kind == str(kind))
        if source:
            filters.append(TaskItemRecord.source == source)
        if query:
            filters.append(TaskItemRecord.title.ilike(f"%{query}%"))
        return filters

    async def admin_list_tasks(
        self,
        *,
        user_id: UUID | None = None,
        status: TaskStatus | None = None,
        kind: TaskKind | None = None,
        source: str | None = None,
        query: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[TaskView]:
        """Admin 可视化列表：跨用户分页，支持状态/类型/来源/标题筛选。"""
        statement = select(TaskItemRecord)
        for clause in self._admin_filters(
            user_id=user_id, status=status, kind=kind, source=source, query=query
        ):
            statement = statement.where(clause)
        statement = (
            statement.order_by(TaskItemRecord.created_at.desc()).limit(limit).offset(offset)
        )
        async with self._database.sessions() as session:
            records = (await session.execute(statement)).scalars().all()
        return [_to_view(record) for record in records]

    async def admin_count_tasks(
        self,
        *,
        user_id: UUID | None = None,
        status: TaskStatus | None = None,
        kind: TaskKind | None = None,
        source: str | None = None,
        query: str | None = None,
    ) -> int:
        statement = select(func.count()).select_from(TaskItemRecord)
        for clause in self._admin_filters(
            user_id=user_id, status=status, kind=kind, source=source, query=query
        ):
            statement = statement.where(clause)
        async with self._database.sessions() as session:
            return int(await session.scalar(statement) or 0)

    async def admin_status_counts(self, *, user_id: UUID | None = None) -> dict[str, int]:
        """按状态聚合的任务计数，用于 Admin 概览卡片。"""
        statement = (
            select(TaskItemRecord.status, func.count())
            .select_from(TaskItemRecord)
            .group_by(TaskItemRecord.status)
        )
        if user_id is not None:
            statement = statement.where(TaskItemRecord.user_id == user_id)
        async with self._database.sessions() as session:
            rows = (await session.execute(statement)).all()
        return {str(row[0]): int(row[1]) for row in rows}

    async def get_task(self, user_id: UUID, task_id: UUID) -> TaskView:
        record = await self._get_record(user_id, task_id)
        return _to_view(record)

    async def find_by_source_ref(self, user_id: UUID, source_ref: str) -> TaskView | None:
        query = select(TaskItemRecord).where(
            TaskItemRecord.user_id == user_id,
            TaskItemRecord.source_ref == source_ref,
        )
        async with self._database.sessions() as session:
            record = await session.scalar(query)
        return _to_view(record) if record is not None else None

    async def complete_task(
        self,
        user_id: UUID,
        task_id: UUID,
        *,
        now: datetime | None = None,
    ) -> TaskView:
        record = await self._get_record(user_id, task_id)
        if record.status not in {str(TaskStatus.ACTIVE), str(TaskStatus.FIRING)}:
            raise ValueError(f"任务当前状态 {record.status} 不能标记完成")
        moment = now or datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            await session.execute(
                update(TaskItemRecord)
                .where(
                    TaskItemRecord.id == task_id,
                    TaskItemRecord.user_id == user_id,
                    TaskItemRecord.status.in_([str(TaskStatus.ACTIVE), str(TaskStatus.FIRING)]),
                )
                .values(
                    status=str(TaskStatus.DONE),
                    completed_at=moment,
                    next_fire_at=None,
                    updated_at=moment,
                )
            )
        updated = await self._get_record(user_id, task_id)
        return _to_view(updated)

    async def cancel_task(
        self,
        user_id: UUID,
        task_id: UUID,
        *,
        now: datetime | None = None,
    ) -> TaskView:
        record = await self._get_record(user_id, task_id)
        if record.status != str(TaskStatus.ACTIVE):
            raise ValueError(f"任务当前状态 {record.status} 不能取消")
        moment = now or datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            await session.execute(
                update(TaskItemRecord)
                .where(
                    TaskItemRecord.id == task_id,
                    TaskItemRecord.user_id == user_id,
                    TaskItemRecord.status == str(TaskStatus.ACTIVE),
                )
                .values(
                    status=str(TaskStatus.CANCELLED),
                    cancelled_at=moment,
                    next_fire_at=None,
                    updated_at=moment,
                )
            )
        updated = await self._get_record(user_id, task_id)
        return _to_view(updated)

    async def snooze_task(
        self,
        user_id: UUID,
        task_id: UUID,
        *,
        until: datetime,
        now: datetime | None = None,
    ) -> TaskView:
        record = await self._get_record(user_id, task_id)
        if record.status != str(TaskStatus.ACTIVE):
            raise ValueError(f"任务当前状态 {record.status} 不能稍后提醒")
        if record.trigger_type != "time":
            raise ValueError("event 触发的任务不支持稍后提醒")
        moment = now or datetime.now(UTC)
        if until <= moment:
            raise ValueError("稍后提醒的时间必须在未来")
        async with self._database.sessions.begin() as session:
            await session.execute(
                update(TaskItemRecord)
                .where(
                    TaskItemRecord.id == task_id,
                    TaskItemRecord.user_id == user_id,
                    TaskItemRecord.status == str(TaskStatus.ACTIVE),
                )
                .values(next_fire_at=until, updated_at=moment)
            )
        updated = await self._get_record(user_id, task_id)
        return _to_view(updated)

    async def update_fields(
        self,
        user_id: UUID,
        task_id: UUID,
        *,
        priority: int | None = None,
        clear_priority: bool = False,
        defer_until: datetime | None = None,
        now: datetime | None = None,
    ) -> TaskView:
        """TODO-01 反向推送的本地入口：优先级与延期。

        - priority：任意任务可改；镜像任务的变更由 TodoSyncService 推回 pnkx；
        - defer_until：仅外部镜像任务接受（本地任务用 snooze 语义）；
          镜像的 next_fire_at 只是"待推送的延期指令"标记，调度器不会本地触发。
        """
        record = await self._get_record(user_id, task_id)
        if record.status != str(TaskStatus.ACTIVE):
            raise ValueError(f"任务当前状态 {record.status} 不能修改")
        if defer_until is not None:
            if record.source != "pnkx":
                raise ValueError("延期仅支持外部镜像任务；本地任务请使用稍后提醒")
            moment = now or datetime.now(UTC)
            if defer_until <= moment:
                raise ValueError("延期时间必须在未来")
        if priority is not None and not 0 <= priority <= 3:
            raise ValueError("priority 取值范围为 0~3")
        moment = now or datetime.now(UTC)
        values: dict[str, object] = {"updated_at": moment}
        if clear_priority:
            values["priority"] = None
            if record.source == "pnkx":
                values["pending_priority"] = None
        elif priority is not None:
            values["priority"] = priority
            if record.source == "pnkx":
                values["pending_priority"] = priority
        if defer_until is not None:
            values["next_fire_at"] = defer_until
        async with self._database.sessions.begin() as session:
            await session.execute(
                update(TaskItemRecord)
                .where(
                    TaskItemRecord.id == task_id,
                    TaskItemRecord.user_id == user_id,
                    TaskItemRecord.status == str(TaskStatus.ACTIVE),
                )
                .values(**values)
            )
        updated = await self._get_record(user_id, task_id)
        return _to_view(updated)

    async def claim_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 20,
    ) -> list[ClaimedTask]:
        """认领所有到期的时间触发任务（跨用户，调度器全局调用）。

        pnkx 镜像永不本地触发（pnkx 自带 Quartz 提醒，双端通知是红线）；
        镜像上的 next_fire_at 是待推送回 pnkx 的延期指令标记。
        """
        moment = now or datetime.now(UTC)
        async with self._database.sessions() as session:
            candidates = (
                (
                    await session.execute(
                        select(TaskItemRecord)
                        .where(
                            TaskItemRecord.trigger_type == "time",
                            TaskItemRecord.status == str(TaskStatus.ACTIVE),
                            TaskItemRecord.source != "pnkx",
                            TaskItemRecord.next_fire_at.is_not(None),
                            TaskItemRecord.next_fire_at <= moment,
                        )
                        .order_by(TaskItemRecord.next_fire_at)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        claimed: list[ClaimedTask] = []
        for record in candidates:
            task = await self._claim(record, moment)
            if task is not None:
                claimed.append(task)
        return claimed

    async def claim_event(
        self,
        user_id: UUID,
        event_type: str,
        *,
        now: datetime | None = None,
        limit: int = 20,
    ) -> list[ClaimedTask]:
        """认领匹配某语义事件的事件触发任务（受 cooldown 限制）。"""
        moment = now or datetime.now(UTC)
        async with self._database.sessions() as session:
            candidates = (
                (
                    await session.execute(
                        select(TaskItemRecord)
                        .where(
                            TaskItemRecord.user_id == user_id,
                            TaskItemRecord.trigger_type == "event",
                            TaskItemRecord.status == str(TaskStatus.ACTIVE),
                            TaskItemRecord.event_type == event_type,
                        )
                        .order_by(TaskItemRecord.created_at)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        claimed: list[ClaimedTask] = []
        for record in candidates:
            trigger = TaskTrigger.model_validate(record.trigger_config)
            cooldown = trigger.cooldown_seconds or 300
            if (
                record.last_fired_at is not None
                and _aware(record.last_fired_at) + timedelta(seconds=cooldown) > moment
            ):
                continue
            task = await self._claim(record, moment)
            if task is not None:
                claimed.append(task)
        return claimed

    async def finish_firing(
        self,
        task_id: UUID,
        *,
        delivery: dict[str, object],
        now: datetime | None = None,
    ) -> None:
        """一次性任务投递结束后转 done；周期任务仅记录投递结果。"""
        moment = now or datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            await session.execute(
                update(TaskItemRecord)
                .where(TaskItemRecord.id == task_id)
                .values(
                    last_delivery=delivery,
                    updated_at=moment,
                )
            )
            await session.execute(
                update(TaskItemRecord)
                .where(
                    TaskItemRecord.id == task_id,
                    TaskItemRecord.status == str(TaskStatus.FIRING),
                )
                .values(
                    status=str(TaskStatus.DONE),
                    completed_at=moment,
                    next_fire_at=None,
                )
            )

    async def recover_interrupted(self, *, now: datetime | None = None) -> int:
        """启动恢复：上次进程中断遗留的 firing 任务直接判完成，不重复投递。"""
        moment = now or datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                update(TaskItemRecord)
                .where(TaskItemRecord.status == str(TaskStatus.FIRING))
                .values(
                    status=str(TaskStatus.DONE),
                    completed_at=moment,
                    next_fire_at=None,
                    last_delivery={"reason_code": "interrupted"},
                    updated_at=moment,
                )
            )
        return int(cast(CursorResult[Any], result).rowcount or 0)

    async def _claim(self, record: TaskItemRecord, moment: datetime) -> ClaimedTask | None:
        trigger = TaskTrigger.model_validate(record.trigger_config)
        next_fire = (
            compute_next_fire(trigger, after=moment) if record.trigger_type == "time" else None
        )
        oneshot = record.trigger_type == "time" and next_fire is None
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                update(TaskItemRecord)
                .where(
                    TaskItemRecord.id == record.id,
                    TaskItemRecord.status == str(TaskStatus.ACTIVE),
                    TaskItemRecord.fire_count == record.fire_count,
                )
                .values(
                    status=str(TaskStatus.FIRING) if oneshot else str(TaskStatus.ACTIVE),
                    last_fired_at=moment,
                    fire_count=record.fire_count + 1,
                    next_fire_at=next_fire,
                    updated_at=moment,
                )
            )
        if not int(cast(CursorResult[Any], result).rowcount or 0):
            return None
        return ClaimedTask(
            id=record.id,
            user_id=record.user_id,
            kind=TaskKind(record.kind),
            title=record.title,
            notes=record.notes,
            oneshot=oneshot,
            privacy_level=PrivacyLevel(record.privacy_level),
            fired_at=moment,
        )

    async def _get_record(self, user_id: UUID, task_id: UUID) -> TaskItemRecord:
        async with self._database.sessions() as session:
            record = await session.get(TaskItemRecord, task_id)
        if record is None or record.user_id != user_id:
            raise LookupError("任务不存在")
        return record
