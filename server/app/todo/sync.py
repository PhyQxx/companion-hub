"""TODO-01 任务同步引擎：pnkx 为单一真源，task_item 作本地镜像。

同步语义（对应验收"双向同步不重复"）：
- 拉取：全量分页拉 pnkx → 按 source_ref=pnkx:{id} upsert 镜像（跳过子任务），
  external_updated_at 不一致才更新；远端已删除 → 本地镜像取消；
- 推完成：本地镜像被标记 done 而远端未完成 → PUT status=1（延期/优先级
  的推送待后续；删除远端是破坏性操作，v1 不自动做）；
- 推新建：本地经 Aria API 手建的无 source_ref 任务 → POST 到 pnkx，
  clientUuid=aria:{task_id}；若上一轮推送后崩溃未回填，本轮拉取快照里
  能按 clientUuid 找到它并直接认领，绝不重复创建；
- 回环防护：镜像 source=pnkx 不会被当作"新建"推送；拉取产生的本地变更
  也不会触发完成推送（远端状态已一致）。

镜像不自动建提醒：pnkx 自带 Quartz 提醒，镜像行 next_fire_at 恒为 NULL，
TaskScheduler 的到期认领天然跳过它们。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update

from app.db import AppUserRecord, Database, TaskItemRecord
from app.ids import uuid7

from .pnkx_client import PnkxTodo, PnkxTodoClient

logger = logging.getLogger("app.todo.sync")

SOURCE_PNKX = "pnkx"
ARIA_CLIENT_PREFIX = "aria:"


@dataclass(slots=True)
class SyncStats:
    pulled: int = 0
    mirrors_created: int = 0
    mirrors_updated: int = 0
    mirrors_cancelled: int = 0
    completions_pushed: int = 0
    new_pushed: int = 0
    adopted_after_crash: int = 0
    errors: list[str] = field(default_factory=list)


def _ref(external_id: str) -> str:
    return f"pnkx:{external_id}"


class TodoSyncService:
    def __init__(
        self,
        database: Database,
        client: PnkxTodoClient,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))

    async def default_user_id(self) -> UUID | None:
        """pnkx 账号与 Aria 主用户对应；单用户部署取首个活跃用户。"""
        async with self._database.sessions() as session:
            value = await session.scalar(
                select(AppUserRecord.id)
                .where(AppUserRecord.status == "active")
                .order_by(AppUserRecord.created_at)
                .limit(1)
            )
        return value

    async def sync_once(self) -> SyncStats:
        stats = SyncStats()
        user_id = await self.default_user_id()
        if user_id is None:
            stats.errors.append("no_active_user")
            return stats
        try:
            all_remote = await self._client.list_all()
        except Exception as error:
            stats.errors.append(f"pull_failed:{type(error).__name__}")
            logger.warning("todo sync pull failed: %s", error)
            return stats
        stats.pulled = len(all_remote)
        remote_items = [item for item in all_remote if not item.is_subtask]

        local_by_ref = await self._local_tasks(user_id)
        remote_by_ref = {_ref(item.external_id): item for item in remote_items}
        remote_by_client_uuid = {
            item.client_uuid: item for item in remote_items if item.client_uuid
        }

        # 1. 拉取 upsert + 删除检测
        for ref, item in remote_by_ref.items():
            local = local_by_ref.get(ref)
            if local is None:
                await self._create_mirror(user_id, item)
                stats.mirrors_created += 1
            elif (local.external_updated_at is None) or (
                item.updated_at is not None
                and item.updated_at.timestamp() != _ts(local.external_updated_at)
            ):
                await self._update_mirror(local, item)
                stats.mirrors_updated += 1
        for ref, local in local_by_ref.items():
            if ref not in remote_by_ref and local.status == "active":
                await self._cancel_mirror(local.id)
                stats.mirrors_cancelled += 1

        # 2. 推完成（本地 done、远端未完成）
        for ref, done_local in local_by_ref.items():
            if done_local.status != "done":
                continue
            remote_done = remote_by_ref.get(ref)
            if remote_done is None or remote_done.status:
                continue
            try:
                await self._client.update(
                    remote_done.external_id,
                    status=True,
                    finish_time=done_local.completed_at or self._clock(),
                )
                stats.completions_pushed += 1
            except Exception as error:
                stats.errors.append(f"push_completion_failed:{ref}")
                logger.warning("todo completion push failed for %s: %s", ref, error)

        # 3. 推新建（本地手建、无 source_ref），崩溃后按 clientUuid 认领
        for local in await self._local_manual_tasks(user_id):
            client_uuid = f"{ARIA_CLIENT_PREFIX}{local.id}"
            existing = remote_by_client_uuid.get(client_uuid)
            if existing is not None:
                await self._backfill_source_ref(local.id, existing)
                stats.adopted_after_crash += 1
                continue
            try:
                external_id = await self._client.create(
                    content=local.title,
                    client_uuid=client_uuid,
                    priority=local.priority,
                    label=local.group_label,
                )
            except Exception as error:
                stats.errors.append(f"push_new_failed:{local.id}")
                logger.warning("todo new push failed for %s: %s", local.id, error)
                continue
            await self._backfill_source_ref(local.id, None, external_id=external_id)
            stats.new_pushed += 1
        return stats

    async def _local_tasks(self, user_id: UUID) -> dict[str, TaskItemRecord]:
        async with self._database.sessions() as session:
            records = (
                await session.scalars(
                    select(TaskItemRecord)
                    .where(
                        TaskItemRecord.user_id == user_id,
                        TaskItemRecord.source == SOURCE_PNKX,
                        TaskItemRecord.source_ref.is_not(None),
                    )
                    .limit(2000)
                )
            ).all()
        return {record.source_ref: record for record in records if record.source_ref}

    async def _local_manual_tasks(self, user_id: UUID) -> list[TaskItemRecord]:
        """待推送的新建任务：用户手建、无外部关联、未完成。"""
        async with self._database.sessions() as session:
            records = (
                await session.scalars(
                    select(TaskItemRecord)
                    .where(
                        TaskItemRecord.user_id == user_id,
                        TaskItemRecord.source == "manual",
                        TaskItemRecord.kind == "task",
                        TaskItemRecord.status == "active",
                    )
                    .order_by(TaskItemRecord.created_at)
                    .limit(100)
                )
            ).all()
        return list(records)

    async def _create_mirror(self, user_id: UUID, item: PnkxTodo) -> None:
        now = self._clock()
        async with self._database.sessions.begin() as session:
            session.add(
                TaskItemRecord(
                    id=uuid7(),
                    user_id=user_id,
                    kind="task",
                    title=item.content[:320] or "(无内容)",
                    status="done" if item.status else "active",
                    trigger_type="time",
                    trigger_config={"type": "time", "at": None, "repeat_kind": "once"},
                    # 镜像永不触发本地提醒：next_fire_at 恒为 NULL
                    next_fire_at=None,
                    source_ref=_ref(item.external_id),
                    external_updated_at=_utc(item.updated_at),
                    priority=item.priority,
                    group_label=item.label[:64] if item.label else None,
                    privacy_level="L1",
                    source=SOURCE_PNKX,
                    completed_at=now if item.status else None,
                    created_at=now,
                    updated_at=now,
                )
            )

    async def _update_mirror(self, local: TaskItemRecord, item: PnkxTodo) -> None:
        now = self._clock()
        async with self._database.sessions.begin() as session:
            await session.execute(
                update(TaskItemRecord)
                .where(TaskItemRecord.id == local.id)
                .values(
                    title=item.content[:320] or local.title,
                    status="done" if item.status else local.status,
                    external_updated_at=_utc(item.updated_at),
                    priority=item.priority,
                    group_label=item.label[:64] if item.label else None,
                    completed_at=local.completed_at or (now if item.status else None),
                    updated_at=now,
                )
            )

    async def _cancel_mirror(self, task_id: UUID) -> None:
        now = self._clock()
        async with self._database.sessions.begin() as session:
            await session.execute(
                update(TaskItemRecord)
                .where(TaskItemRecord.id == task_id, TaskItemRecord.status == "active")
                .values(status="cancelled", cancelled_at=now, updated_at=now)
            )

    async def _backfill_source_ref(
        self,
        task_id: UUID,
        adopted: PnkxTodo | None,
        *,
        external_id: str | None = None,
    ) -> None:
        now = self._clock()
        resolved = adopted.external_id if adopted is not None else external_id
        assert resolved is not None
        async with self._database.sessions.begin() as session:
            await session.execute(
                update(TaskItemRecord)
                .where(TaskItemRecord.id == task_id)
                .values(
                    source=SOURCE_PNKX,
                    source_ref=_ref(resolved),
                    external_updated_at=adopted.updated_at if adopted is not None else now,
                    updated_at=now,
                )
            )


def _utc(value: datetime | None) -> datetime | None:
    """pnkx 时间带 Asia/Shanghai 偏移；入库统一转 UTC，避免 SQLite 丢偏移后误判变更。"""
    if value is None:
        return None
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _ts(value: datetime | None) -> float:
    if value is None:
        return -1.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()
