"""Defensive validation of declared projection evidence, without persistence."""

from datetime import UTC, datetime
from uuid import UUID

from app.calendar.models import CalendarEventView
from app.cognition.models import GoalStatus, GoalView
from app.schemas.common import PrivacyLevel


def bounded_text(text: str, maximum: int) -> str:
    return text if len(text) <= maximum else text[: maximum - 1] + "…"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def accepted_events(
    events: list[CalendarEventView],
    owner: UUID,
    start: datetime,
    end: datetime,
) -> list[CalendarEventView]:
    return [
        event.model_copy(
            deep=True,
            update={"starts_at": _aware(event.starts_at), "ends_at": _aware(event.ends_at)},
        )
        for event in events
        if event.user_id == owner
        and event.status == "active"
        and start <= _aware(event.starts_at) < end
        and _aware(event.ends_at) > _aware(event.starts_at)
    ]


def accepted_goals(
    goals: list[GoalView],
    *,
    now: datetime,
    active: bool,
    completed: bool = False,
) -> list[GoalView]:
    return [
        goal.model_copy(deep=True)
        for goal in goals
        if goal.privacy_level in {None, PrivacyLevel.L0, PrivacyLevel.L1}
        and (not completed or goal.status == GoalStatus.COMPLETED)
        and (
            not active
            or (
                goal.status == GoalStatus.ACTIVE
                and (goal.expires_at is None or _aware(goal.expires_at) > now)
            )
        )
    ]
