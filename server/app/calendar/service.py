"""CAL-01 日历服务：写入前预览、冲突检查与会前提醒联动。

验收「写入前展示时间、参与人和目标日历」：preview 是纯读操作，返回
规范化后的事件（时间窗、参与人、目标日历）与冲突列表，不产生任何写入；
真正的 create/patch/cancel 是显式 API，不接入模型工具直呼。

会前提醒复用 TASK-01 调度底座：事件以 source_ref=calendar:{id} 关联
提醒任务，改期撤旧建新、取消撤旧，不额外引入新调度器。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.schemas.common import PrivacyLevel
from app.tasks.models import TaskKind, TaskStatus, TaskTrigger
from app.tasks.store import TaskStore

from .models import CalendarEventView, CalendarParticipant, CalendarPreview
from .store import CalendarStore, _to_view

DEFAULT_REMINDER_LEAD_MINUTES = 10
MIN_REMINDER_LEAD_MINUTES = 0
MAX_REMINDER_LEAD_MINUTES = 24 * 60


class CalendarService:
    def __init__(
        self,
        store: CalendarStore,
        task_store: TaskStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._tasks = task_store
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def store(self) -> CalendarStore:
        return self._store

    @property
    def task_store(self) -> TaskStore:
        return self._tasks

    async def preview(
        self,
        user_id: UUID,
        *,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        calendar_id: str = "primary",
        location: str | None = None,
        participants: list[CalendarParticipant] | None = None,
        reminder_lead_minutes: int = DEFAULT_REMINDER_LEAD_MINUTES,
    ) -> CalendarPreview:
        """写入前展示：规范化 + 冲突检查，纯读不落库。"""
        _validate_window(starts_at, ends_at)
        _validate_lead(reminder_lead_minutes)
        conflicts = await self._store.overlapping(user_id, starts_at=starts_at, ends_at=ends_at)
        return CalendarPreview(
            title=title,
            calendar_id=calendar_id,
            starts_at=starts_at,
            ends_at=ends_at,
            location=location,
            participants=participants or [],
            reminder_lead_minutes=reminder_lead_minutes,
            conflicts=conflicts,
        )

    async def create_event(
        self,
        user_id: UUID,
        *,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        calendar_id: str = "primary",
        notes: str | None = None,
        location: str | None = None,
        participants: list[CalendarParticipant] | None = None,
        reminder_lead_minutes: int = DEFAULT_REMINDER_LEAD_MINUTES,
    ) -> CalendarEventView:
        _validate_window(starts_at, ends_at)
        _validate_lead(reminder_lead_minutes)
        record = await self._store.create_event(
            user_id=user_id,
            title=title,
            starts_at=starts_at,
            ends_at=ends_at,
            calendar_id=calendar_id,
            notes=notes,
            location=location,
            participants=participants,
        )
        await self._sync_reminder(user_id, record.id, title, starts_at, reminder_lead_minutes)
        return await self._view_with_reminder(user_id, record.id)

    async def reschedule_event(
        self,
        user_id: UUID,
        event_id: UUID,
        *,
        title: str | None = None,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
        location: str | None = None,
        notes: str | None = None,
        reminder_lead_minutes: int | None = None,
    ) -> CalendarEventView:
        updated = await self._store.update_event(
            user_id,
            event_id,
            title=title,
            starts_at=starts_at,
            ends_at=ends_at,
            location=location,
            notes=notes,
        )
        # 改期后重建提醒：撤旧建新，避免旧时间点误提醒
        await self._tasks.cancel_tasks_by_source_ref(user_id, _ref(event_id))
        await self._sync_reminder(
            user_id,
            event_id,
            updated.title,
            updated.starts_at,
            DEFAULT_REMINDER_LEAD_MINUTES
            if reminder_lead_minutes is None
            else reminder_lead_minutes,
        )
        return await self._view_with_reminder(user_id, event_id)

    async def cancel_event(self, user_id: UUID, event_id: UUID) -> CalendarEventView:
        cancelled = await self._store.cancel_event(user_id, event_id)
        await self._tasks.cancel_tasks_by_source_ref(user_id, _ref(event_id))
        return cancelled.model_copy(update={"reminder_task_id": None})

    async def list_events(self, user_id: UUID, **kwargs: object) -> list[CalendarEventView]:
        return await self._store.list_events(user_id, **kwargs)  # type: ignore[arg-type]

    async def _sync_reminder(
        self,
        user_id: UUID,
        event_id: UUID,
        title: str,
        starts_at: datetime,
        lead_minutes: int,
    ) -> None:
        if lead_minutes <= 0:
            return
        moment = self._clock()
        fire_at = starts_at - timedelta(minutes=lead_minutes)
        if fire_at <= moment:
            # 事件太近，提前量已过：改为尽快提醒（仍经任务调度 exactly-once）
            fire_at = moment + timedelta(seconds=1)
        await self._tasks.create(
            user_id=user_id,
            kind=TaskKind.REMINDER,
            title=f"日程提醒：{title}（{starts_at.strftime('%m-%d %H:%M')}）",
            trigger=TaskTrigger(type="time", at=fire_at),
            privacy_level=PrivacyLevel.L1,
            source="calendar",
            source_ref=_ref(event_id),
            now=moment,
        )

    async def _view_with_reminder(self, user_id: UUID, event_id: UUID) -> CalendarEventView:
        record = await self._store.get_event(user_id, event_id)
        view = _to_view(record)
        reminder_id: UUID | None = None
        for task in await self._tasks.list_tasks(user_id, limit=500):
            if task.source_ref == _ref(event_id) and task.status == TaskStatus.ACTIVE:
                reminder_id = task.id
                break
        return view.model_copy(update={"reminder_task_id": reminder_id})


def _ref(event_id: UUID) -> str:
    return f"calendar:{event_id}"


def _validate_window(starts_at: datetime, ends_at: datetime) -> None:
    if ends_at <= starts_at:
        raise ValueError("结束时间必须晚于开始时间")


def _validate_lead(lead_minutes: int) -> None:
    if not MIN_REMINDER_LEAD_MINUTES <= lead_minutes <= MAX_REMINDER_LEAD_MINUTES:
        raise ValueError("会前提醒提前量必须在 0-1440 分钟之间")
