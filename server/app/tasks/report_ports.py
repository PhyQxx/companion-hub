"""Report sources return owned L1-visible projections before pagination.

GoalView has no owner or created/completed timestamps: repositories must enforce
these scopes, including inherited privacy, rather than inventing DTO evidence.
"""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.calendar.models import CalendarEventView
from app.cognition.models import GoalView
from app.contacts.models import ContactView
from app.schemas.common import PrivacyLevel
from app.tasks.models import TaskStatus, TaskView


class ReportTaskQuery(Protocol):
    async def list_tasks(
        self,
        user_id: UUID,
        *,
        status: TaskStatus | None = None,
        limit: int = 200,
        max_privacy_level: PrivacyLevel | None = None,
    ) -> list[TaskView]: ...


class ReportGoalQuery(Protocol):
    async def active_goals(
        self,
        user_id: UUID,
        *,
        now: datetime,
        max_privacy_level: PrivacyLevel | None = None,
    ) -> list[GoalView]: ...

    async def goals_completed_between(
        self,
        user_id: UUID,
        *,
        start: datetime,
        end: datetime,
        limit: int = 50,
        max_privacy_level: PrivacyLevel | None = None,
    ) -> list[GoalView]: ...

    async def goals_created_between(
        self,
        user_id: UUID,
        *,
        start: datetime,
        end: datetime,
        limit: int = 50,
        max_privacy_level: PrivacyLevel | None = None,
    ) -> list[GoalView]: ...


class ReportCalendarQuery(Protocol):
    async def list_events(
        self,
        user_id: UUID,
        *,
        starts_from: datetime | None = None,
        starts_to: datetime | None = None,
        include_cancelled: bool = False,
        limit: int = 200,
    ) -> list[CalendarEventView]: ...


class ReportContactQuery(Protocol):
    async def contacts_with_date(
        self,
        user_id: UUID,
        *,
        month: int,
        day: int,
    ) -> list[ContactView]: ...
