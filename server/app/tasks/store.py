"""TASK-01 任务存储：CRUD、稍后/完成/取消与 exactly-once 触发认领。

认领语义：候选行先 SELECT，再用 `status='active' AND fire_count=<读到的值>`
做乐观 UPDATE；rowcount=0 视为并发方已认领而跳过。一次性任务认领后进入
firing，投递结束由调用方 finish；进程中断遗留的 firing 由 recover 处理，
宁可少投一次也不重复投递。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.calendar.models import CalendarEventView, CalendarSourceInvalidated
from app.db import AppUserRecord, CalendarEventRecord, Database, TaskItemRecord, TaskRunRecord
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

    async def _cancel_current_delivery(
        self, session: AsyncSession, user_id: UUID, task_id: UUID
    ) -> None:
        from app.runs.store import transition_run

        source = await session.get(TaskItemRecord, task_id)
        if source is None or source.user_id != user_id:
            return
        raw_id = (source.last_delivery or {}).get("run_id")
        try:
            run_id = UUID(str(raw_id)) if raw_id else None
        except ValueError:
            return
        if run_id is None:
            return
        root = await session.get(TaskRunRecord, run_id, with_for_update=True)
        if (
            root is not None
            and root.user_id == user_id
            and root.contract.get("source_id") == str(task_id)
            and root.contract.get("criterion") == "delivery_channels_returned"
        ):
            await transition_run(session, root.id, "cancelled")

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

    async def cancel_tasks_by_source_refs_in_session(
        self,
        session: AsyncSession,
        user_id: UUID,
        source_refs: Sequence[str],
        *,
        now: datetime,
        include_dependents: bool = True,
    ) -> int:
        refs = set(source_refs)
        if include_dependents:
            for ref in tuple(refs):
                if not ref.startswith("calendar:"):
                    continue
                try:
                    event_id = UUID(ref.removeprefix("calendar:"))
                except ValueError:
                    continue
                refs.add(f"commute:{event_id}")
        ordered_refs = sorted(refs)
        identifiers: set[UUID] = set()
        for offset in range(0, len(ordered_refs), 500):
            result = await session.execute(
                update(TaskItemRecord)
                .where(
                    TaskItemRecord.user_id == user_id,
                    TaskItemRecord.source_ref.in_(ordered_refs[offset : offset + 500]),
                    TaskItemRecord.status.in_([str(TaskStatus.ACTIVE), str(TaskStatus.FIRING)]),
                )
                .values(
                    status=str(TaskStatus.CANCELLED),
                    cancelled_at=now,
                    next_fire_at=None,
                    updated_at=now,
                )
                .returning(TaskItemRecord.id)
            )
            identifiers.update(result.scalars())
        for identifier in sorted(identifiers):
            await self._cancel_current_delivery(session, user_id, identifier)
        return len(identifiers)

    async def cancel_tasks_by_source_ref_in_session(
        self,
        session: AsyncSession,
        user_id: UUID,
        source_ref: str,
        *,
        now: datetime,
        include_dependents: bool = True,
    ) -> int:
        return await self.cancel_tasks_by_source_refs_in_session(
            session, user_id, (source_ref,), now=now, include_dependents=include_dependents
        )

    async def cancel_tasks_by_source_ref(self, user_id: UUID, source_ref: str) -> int:
        """Cancel owned source tasks and calendar-dependent departures in one transaction."""
        async with self._database.sessions.begin() as session:
            return await self.cancel_tasks_by_source_ref_in_session(
                session, user_id, source_ref, now=datetime.now(UTC)
            )

    async def replace_calendar_reminder(
        self,
        user_id: UUID,
        event: CalendarEventView,
        *,
        title: str,
        trigger: TaskTrigger,
        source: str,
        now: datetime,
    ) -> TaskView:
        """Fence current source, cancel old task and insert its replacement atomically."""
        event = event.model_copy(deep=True)
        if (
            source not in ("calendar", "commute")
            or event.user_id != user_id
            or event.status != "active"
        ):
            raise CalendarSourceInvalidated("calendar_source_changed")
        if trigger.type != "time":
            raise ValueError("calendar reminders require a time trigger")
        normalized = validate_trigger(trigger.model_copy(deep=True), now=now)
        ref = f"{source}:{event.id}"
        async with self._database.sessions.begin() as session:
            active_owner = await session.scalar(
                update(AppUserRecord)
                .where(AppUserRecord.id == user_id, AppUserRecord.status == "active")
                .values(status=AppUserRecord.status)
                .returning(AppUserRecord.id)
            )
            if active_owner is None:
                raise CalendarSourceInvalidated("task_owner_inactive")
            current = await session.scalar(
                update(CalendarEventRecord)
                .where(
                    CalendarEventRecord.id == event.id,
                    CalendarEventRecord.user_id == user_id,
                    CalendarEventRecord.status == "active",
                    CalendarEventRecord.starts_at == event.starts_at,
                    CalendarEventRecord.ends_at == event.ends_at,
                    CalendarEventRecord.title == event.title,
                    CalendarEventRecord.location == event.location,
                    CalendarEventRecord.updated_at == event.updated_at,
                )
                .values(updated_at=CalendarEventRecord.updated_at)
                .returning(CalendarEventRecord.id)
            )
            if current is None:
                raise CalendarSourceInvalidated("calendar_source_changed")
            await self.cancel_tasks_by_source_ref_in_session(
                session, user_id, ref, now=now, include_dependents=False
            )
            record = TaskItemRecord(
                id=uuid7(),
                user_id=user_id,
                kind=str(TaskKind.REMINDER),
                title=title,
                notes=None,
                status=str(TaskStatus.ACTIVE),
                trigger_type=str(normalized.type),
                trigger_config=normalized.model_dump(mode="json"),
                event_type=normalized.event_type,
                source_ref=ref,
                next_fire_at=normalized.at if normalized.type == "time" else None,
                fire_count=0,
                privacy_level=str(PrivacyLevel.L1),
                source=source,
                created_at=now,
                updated_at=now,
            )
            session.add(record)
        return _to_view(record)

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
        statement = statement.order_by(TaskItemRecord.created_at.desc()).limit(limit).offset(offset)
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
            await self._cancel_current_delivery(session, user_id, task_id)
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
        if record.status not in {str(TaskStatus.ACTIVE), str(TaskStatus.FIRING)}:
            raise ValueError(f"任务当前状态 {record.status} 不能取消")
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
                    status=str(TaskStatus.CANCELLED),
                    cancelled_at=moment,
                    next_fire_at=None,
                    updated_at=moment,
                )
            )
            await self._cancel_current_delivery(session, user_id, task_id)
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
        if record.status not in {str(TaskStatus.ACTIVE), str(TaskStatus.FIRING)}:
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
                    TaskItemRecord.status.in_([str(TaskStatus.ACTIVE), str(TaskStatus.FIRING)]),
                )
                .values(next_fire_at=until, status=str(TaskStatus.ACTIVE), updated_at=moment)
            )
            await self._cancel_current_delivery(session, user_id, task_id)
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
                            TaskItemRecord.user_id.in_(
                                select(AppUserRecord.id).where(AppUserRecord.status == "active")
                            ),
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
                            TaskItemRecord.user_id.in_(
                                select(AppUserRecord.id).where(AppUserRecord.status == "active")
                            ),
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
        """Do not reclaim another process's live Run or a newly claimed source."""
        moment = now or datetime.now(UTC)
        count = 0
        after: UUID | None = None
        while True:
            query = (
                select(TaskItemRecord.id)
                .where(TaskItemRecord.status == str(TaskStatus.FIRING))
                .order_by(TaskItemRecord.id)
                .limit(100)
            )
            if after is not None:
                query = query.where(TaskItemRecord.id > after)
            async with self._database.sessions() as session:
                identifiers = list(await session.scalars(query))
            if not identifiers:
                return count
            for identifier in identifiers:
                async with self._database.sessions.begin() as session:
                    row = await session.scalar(
                        update(TaskItemRecord)
                        .where(
                            TaskItemRecord.id == identifier,
                            TaskItemRecord.status == str(TaskStatus.FIRING),
                        )
                        .values(updated_at=TaskItemRecord.updated_at)
                        .returning(TaskItemRecord)
                    )
                    if row is None:
                        continue
                    raw_id = (row.last_delivery or {}).get("run_id")
                    try:
                        run_id = UUID(str(raw_id)) if raw_id else None
                    except ValueError:
                        run_id = None
                    run = (
                        await session.get(TaskRunRecord, run_id, with_for_update=True)
                        if run_id
                        else None
                    )
                    if (
                        run is not None
                        and run.status in {"accepted", "running"}
                        and run.deadline is not None
                        and _aware(run.deadline) > moment
                    ):
                        continue
                    if (
                        run_id is not None
                        and run is None
                        and row.last_fired_at is not None
                        and _aware(row.last_fired_at) + timedelta(seconds=180) > moment
                    ):
                        continue
                    row.status, row.completed_at, row.next_fire_at = (
                        str(TaskStatus.DONE),
                        moment,
                        None,
                    )
                    row.last_delivery = (
                        {"reason_code": "interrupted"}
                        if run is None
                        else {
                            "run_id": str(run.id),
                            "fire_count": row.fire_count,
                            "reason_code": "interrupted",
                            "outcome": "unknown"
                            if run.contract.get("dispatch_state") in {"started", "unknown"}
                            else "not_started",
                        }
                    )
                    row.updated_at = moment
                    count += 1
            after = identifiers[-1]

    async def _claim(self, record: TaskItemRecord, moment: datetime) -> ClaimedTask | None:
        run_id = uuid7()
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
                    TaskItemRecord.user_id.in_(
                        select(AppUserRecord.id).where(AppUserRecord.status == "active")
                    ),
                )
                .values(
                    status=str(TaskStatus.FIRING) if oneshot else str(TaskStatus.ACTIVE),
                    last_fired_at=moment,
                    fire_count=record.fire_count + 1,
                    last_delivery={
                        "run_id": str(run_id),
                        "fire_count": record.fire_count + 1,
                        "outcome": "claimed",
                    },
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
            run_id=run_id,
        )

    async def _get_record(self, user_id: UUID, task_id: UUID) -> TaskItemRecord:
        async with self._database.sessions() as session:
            record = await session.get(TaskItemRecord, task_id)
        if record is None or record.user_id != user_id:
            raise LookupError("任务不存在")
        return record
