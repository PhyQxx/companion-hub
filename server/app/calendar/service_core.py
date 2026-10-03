"""CAL-01 日历服务：写入前预览、冲突检查与会前提醒联动。

验收「写入前展示时间、参与人和目标日历」：preview 是纯读操作，返回
规范化后的事件（时间窗、参与人、目标日历）与冲突列表，不产生任何写入；
真正的 create/patch/cancel 是显式 API，不接入模型工具直呼。

会前提醒复用 TASK-01 调度底座：事件以 source_ref=calendar:{id} 关联
提醒任务，改期撤旧建新、取消撤旧，不额外引入新调度器。
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.harness.time import utc
from app.tasks.models import TaskStatus, TaskTrigger

from .models import CalendarEventView, CalendarParticipant, CalendarPreview
from .ports import CalendarReminderWriter, CalendarRepository

DEFAULT_REMINDER_LEAD_MINUTES = 10
MIN_REMINDER_LEAD_MINUTES = 0
MAX_REMINDER_LEAD_MINUTES = 24 * 60


class CalendarCoordinator:
    def __init__(
        self,
        store: CalendarRepository,
        task_store: CalendarReminderWriter,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._tasks = task_store
        self._clock = clock or (lambda: datetime.now(UTC))

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
        participants = deepcopy(participants or [])
        conflicts = await self._store.overlapping(user_id, starts_at=starts_at, ends_at=ends_at)
        conflicts = sorted(
            (
                event.model_copy(deep=True)
                for event in conflicts
                if event.user_id == user_id
                and event.status == "active"
                and utc(event.starts_at) < utc(ends_at)
                and utc(event.ends_at) > utc(starts_at)
            ),
            key=lambda event: utc(event.starts_at),
        )
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
        participants = deepcopy(participants or [])
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
        record = _owned_view(record, user_id, active=True)
        await self._sync_reminder(user_id, record, reminder_lead_minutes)
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
        lead_minutes = (
            DEFAULT_REMINDER_LEAD_MINUTES
            if reminder_lead_minutes is None
            else reminder_lead_minutes
        )
        _validate_lead(lead_minutes)
        if starts_at is not None and ends_at is not None:
            _validate_window(starts_at, ends_at)
        updated = await self._store.update_event(
            user_id,
            event_id,
            title=title,
            starts_at=starts_at,
            ends_at=ends_at,
            location=location,
            notes=notes,
        )
        updated = _owned_view(updated, user_id, event_id, active=True)
        # 改期后重建提醒：撤旧建新，避免旧时间点误提醒
        await self._sync_reminder(
            user_id,
            updated,
            lead_minutes,
        )
        return await self._view_with_reminder(user_id, event_id)

    async def cancel_event(self, user_id: UUID, event_id: UUID) -> CalendarEventView:
        cancelled = await self._store.cancel_event(user_id, event_id)
        return _owned_view(cancelled, user_id, event_id).model_copy(
            update={"reminder_task_id": None}
        )

    async def list_events(
        self,
        user_id: UUID,
        *,
        starts_from: datetime | None = None,
        starts_to: datetime | None = None,
        include_cancelled: bool = False,
        limit: int = 200,
    ) -> list[CalendarEventView]:
        events = await self._store.list_events(
            user_id,
            starts_from=starts_from,
            starts_to=starts_to,
            include_cancelled=include_cancelled,
            limit=limit,
        )
        return [
            event.model_copy(deep=True)
            for event in events
            if event.user_id == user_id
            and (include_cancelled or event.status == "active")
            and (starts_from is None or utc(event.starts_at) >= utc(starts_from))
            and (starts_to is None or utc(event.starts_at) < utc(starts_to))
        ]

    async def _sync_reminder(
        self,
        user_id: UUID,
        event: CalendarEventView,
        lead_minutes: int,
    ) -> None:
        event = _owned_view(event, user_id, active=True)
        if lead_minutes <= 0:
            return
        moment = self._clock()
        fire_at = event.starts_at - timedelta(minutes=lead_minutes)
        if fire_at <= moment:
            # 事件太近，提前量已过：改为尽快提醒（仍经任务调度 exactly-once）
            fire_at = moment + timedelta(seconds=1)
        await self._tasks.replace_calendar_reminder(
            user_id,
            event,
            title=f"日程提醒：{event.title}（{event.starts_at.strftime('%m-%d %H:%M')}）",
            trigger=TaskTrigger(type="time", at=fire_at),
            source="calendar",
            now=moment,
        )

    async def _view_with_reminder(self, user_id: UUID, event_id: UUID) -> CalendarEventView:
        view = _owned_view(await self._store.get_event(user_id, event_id), user_id, event_id)
        reminder = await self._tasks.find_active_by_source_ref(user_id, _ref(event_id))
        reminder_id = (
            reminder.id
            if reminder is not None
            and reminder.user_id == user_id
            and reminder.source_ref == _ref(event_id)
            and reminder.status == TaskStatus.ACTIVE
            and view.status == "active"
            else None
        )
        return view.model_copy(update={"reminder_task_id": reminder_id})


def _owned_view(
    view: CalendarEventView, user_id: UUID, event_id: UUID | None = None, *, active: bool = False
) -> CalendarEventView:
    if (
        view.user_id != user_id
        or (event_id is not None and view.id != event_id)
        or (active and view.status != "active")
    ):
        raise LookupError("calendar event not found")
    return view.model_copy(deep=True)


def _ref(event_id: UUID) -> str:
    return f"calendar:{event_id}"


def _validate_window(starts_at: datetime, ends_at: datetime) -> None:
    if ends_at <= starts_at:
        raise ValueError("结束时间必须晚于开始时间")


def _validate_lead(lead_minutes: int) -> None:
    if not MIN_REMINDER_LEAD_MINUTES <= lead_minutes <= MAX_REMINDER_LEAD_MINUTES:
        raise ValueError("会前提醒提前量必须在 0-1440 分钟之间")
