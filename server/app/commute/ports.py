"""Detached query, route and reminder capabilities used by commute planning."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.calendar.models import CalendarEventView
from app.schemas.common import PrivacyLevel
from app.tasks.models import TaskKind, TaskTrigger, TaskView


class CommuteAmapProvider(Protocol):
    """出行服务用到的高德能力子集（复用 tools.amap.AmapProvider 实例）。"""

    async def geocode(self, address: str, *, city: str | None = None) -> dict[str, object]: ...

    async def route(
        self,
        origin: str,
        destination: str,
        *,
        mode: str,
        origin_citycode: str | None = None,
        destination_citycode: str | None = None,
    ) -> dict[str, object]: ...


class CommuteCalendarStore(Protocol):
    """出行服务用到的日历能力子集（CalendarStore 满足）。"""

    async def list_events(
        self,
        user_id: UUID,
        *,
        starts_from: datetime | None = None,
        starts_to: datetime | None = None,
        include_cancelled: bool = False,
        limit: int = 200,
    ) -> list[CalendarEventView]: ...


class CommuteTaskStore(Protocol):
    async def cancel_tasks_by_source_ref(self, user_id: UUID, source_ref: str) -> int: ...

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
    ) -> TaskView: ...
