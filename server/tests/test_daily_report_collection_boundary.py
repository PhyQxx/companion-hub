"""Daily report projections are bounded; budget denial is terminal."""

import asyncio
from datetime import timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from test_commute_query_boundary import NOW

from app.calendar.models import CalendarEventView
from app.cognition.models import GoalKind, GoalStatus, GoalView
from app.cognition.store import CognitiveStore
from app.contacts.models import ContactImportantDate, ContactView
from app.contacts.store import ContactStore
from app.db import Database
from app.harness.budget import BudgetDenied
from app.schemas.common import PrivacyLevel
from app.tasks.brief import BriefCommute, BriefWeather, DailyBriefService
from app.tasks.models import TaskKind, TaskStatus, TaskTrigger, TaskView
from app.tasks.review import DailyReviewService
from app.tasks.store import TaskStore


class Sources:
    def __init__(self) -> None:
        self.owner = uuid4()
        self.calls: list[str] = []
        self.failure: tuple[str, BaseException] | None = None
        self.tasks: list[TaskView] = []
        self.goals: list[GoalView] = []
        self.events: list[CalendarEventView] = []
        self.contacts: list[ContactView] = []
        self.weather_value: BriefWeather | None = None
        self.commute_value: BriefCommute | None = None

    def check(self, stage: str) -> None:
        self.calls.append(stage)
        if self.failure is not None and self.failure[0] == stage:
            raise self.failure[1]

    async def list_tasks(self, user_id: UUID, **kwargs: Any) -> list[TaskView]:
        assert user_id == self.owner and kwargs["max_privacy_level"] == PrivacyLevel.L1
        self.check("tasks")
        return self.tasks

    async def active_goals(self, user_id: UUID, **kwargs: Any) -> list[GoalView]:
        assert user_id == self.owner and kwargs["max_privacy_level"] == PrivacyLevel.L1
        self.check("goals")
        return self.goals

    async def goals_completed_between(self, user_id: UUID, **kwargs: Any) -> list[GoalView]:
        assert user_id == self.owner and kwargs["max_privacy_level"] == PrivacyLevel.L1
        self.check("completed_goals")
        return []

    async def goals_created_between(self, user_id: UUID, **kwargs: Any) -> list[GoalView]:
        assert user_id == self.owner and kwargs["max_privacy_level"] == PrivacyLevel.L1
        self.check("new_goals")
        return []

    async def list_events(self, user_id: UUID, **kwargs: Any) -> list[CalendarEventView]:
        assert user_id == self.owner
        self.check("calendar")
        return self.events

    async def contacts_with_date(self, user_id: UUID, **kwargs: Any) -> list[ContactView]:
        assert user_id == self.owner
        self.check("contacts")
        return self.contacts

    async def weather(self) -> BriefWeather | None:
        self.check("weather")
        return self.weather_value

    async def commute(self, user_id: UUID) -> BriefCommute | None:
        assert user_id == self.owner
        self.check("commute")
        return self.commute_value

    def service(self, domain: str) -> DailyBriefService | DailyReviewService:
        if domain == "brief":
            return DailyBriefService(
                cast(Database, None),
                cast(TaskStore, self),
                cast(CognitiveStore, self),
                contact_store=cast(ContactStore, self),
                weather_fetcher=self.weather,
                commute_fetcher=self.commute,
                calendar_store=self,
                clock=lambda: NOW,
            )
        return DailyReviewService(
            cast(Database, None),
            cast(TaskStore, self),
            cast(CognitiveStore, self),
            calendar_store=self,
            clock=lambda: NOW,
        )

    def task(self, title: str = "task fixture") -> TaskView:
        return TaskView(
            id=uuid4(),
            user_id=self.owner,
            kind=TaskKind.REMINDER,
            title=title,
            status=TaskStatus.ACTIVE,
            trigger=TaskTrigger(type="time", at=NOW),
            next_fire_at=NOW,
            privacy_level=PrivacyLevel.L1,
            source="fixture",
        )

    def goal(self, title: str = "goal fixture") -> GoalView:
        return GoalView(
            id=uuid4(),
            kind=GoalKind.USER,
            title=title,
            status=GoalStatus.ACTIVE,
            source_kind="manual",
            source_id="fixture",
            due_at=NOW,
            privacy_level=PrivacyLevel.L1,
        )

    def event(self, domain: str, title: str = "calendar fixture") -> CalendarEventView:
        start = NOW + timedelta(days=domain == "review", hours=1)
        return CalendarEventView(
            id=uuid4(),
            user_id=self.owner,
            calendar_id="primary",
            title=title,
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            status="active",
        )


async def collect(sources: Sources, domain: str) -> list[Any]:
    service = sources.service(domain)
    if isinstance(service, DailyBriefService):
        return await service.collect_facts(sources.owner, brief_date=NOW.date())
    return await service.collect_items(sources.owner, review_date=NOW.date())


@pytest.mark.parametrize(
    "domain,source",
    [
        ("brief", "task"),
        ("review", "task"),
        ("brief", "goal"),
        ("review", "goal"),
        ("brief", "calendar"),
        ("review", "calendar"),
        ("brief", "weather"),
        ("brief", "contact"),
        ("brief", "commute"),
    ],
)
async def test_long_source_text_is_bounded_without_mutating_source(
    domain: str, source: str
) -> None:
    sources = Sources()
    title = "原始用户标题" * 50
    expected_source: str | None = None
    if source == "task":
        sources.tasks = [sources.task(title)]
        expected_source = f"task:{sources.tasks[0].id}"
    elif source == "goal":
        sources.goals = [sources.goal(title)]
        expected_source = f"goal:{sources.goals[0].id}"
    elif source == "calendar":
        sources.events = [sources.event(domain, title)]
        expected_source = f"calendar:{sources.events[0].id}"
    elif source == "weather":
        sources.weather_value = BriefWeather(title, title, "20", "10", "25")
    elif source == "contact":
        sources.contacts = [
            ContactView(
                id=uuid4(),
                user_id=sources.owner,
                display_name=title,
                important_dates=[
                    ContactImportantDate(label="纪念日", month=NOW.month, day=NOW.day)
                ],
            )
        ]
        expected_source = f"contact:{sources.contacts[0].id}"
    else:
        sources.commute_value = BriefCommute(title, NOW, title, NOW, "步行", 20)
    entries = await collect(sources, domain)
    assert len(entries) == 1
    assert len(entries[0].text) <= (240 if domain == "brief" else 280)
    assert entries[0].text.endswith("…") and len(entries[0].source) <= 120
    if expected_source:
        assert entries[0].source == expected_source
    if sources.tasks:
        assert sources.tasks[0].title == title
    if sources.events:
        assert sources.events[0].title == title


@pytest.mark.parametrize(
    "domain,stage",
    [
        ("brief", "weather"),
        ("brief", "calendar"),
        ("brief", "commute"),
        ("review", "calendar"),
    ],
)
@pytest.mark.parametrize("failure", ["budget", "cancel", "ordinary"])
async def test_optional_source_budget_and_cancellation_stop_collection(
    domain: str,
    stage: str,
    failure: str,
) -> None:
    sources = Sources()
    error: BaseException = (
        BudgetDenied("synthetic_budget_denied")
        if failure == "budget"
        else asyncio.CancelledError()
        if failure == "cancel"
        else RuntimeError("offline fixture")
    )
    sources.failure = (stage, error)
    if failure == "ordinary":
        assert await collect(sources, domain) == []
    else:
        with pytest.raises(type(error)) as captured:
            await collect(sources, domain)
        assert captured.value is error
        assert sources.calls[-1] == stage
    assert sources.calls.count(stage) == 1


@pytest.mark.parametrize("domain", ["brief", "review"])
@pytest.mark.parametrize("invalid", ["owner", "cancelled", "before", "after", "duration"])
async def test_calendar_projection_is_rechecked_before_render(domain: str, invalid: str) -> None:
    sources = Sources()
    event = sources.event(domain)
    changes: dict[str, dict[str, Any]] = {
        "owner": {"user_id": uuid4()},
        "cancelled": {"status": "cancelled"},
        "before": {"starts_at": NOW - timedelta(days=2)},
        "after": {"starts_at": NOW + timedelta(days=3)},
        "duration": {"ends_at": event.starts_at},
    }
    sources.events = [event.model_copy(update=changes[invalid])]
    assert await collect(sources, domain) == []


@pytest.mark.parametrize("invalid", ["owner", "date"])
async def test_contact_projection_is_rechecked_before_render(invalid: str) -> None:
    sources = Sources()
    sources.contacts = [
        ContactView(
            id=uuid4(),
            user_id=uuid4() if invalid == "owner" else sources.owner,
            display_name="contact fixture",
            important_dates=[
                ContactImportantDate(
                    label="birthday",
                    month=NOW.month,
                    day=(NOW.day % 28 + 1) if invalid == "date" else NOW.day,
                )
            ],
        )
    ]
    assert await collect(sources, "brief") == []


@pytest.mark.parametrize("domain", ["brief", "review"])
@pytest.mark.parametrize("invalid", ["privacy", "status", "expiry"])
async def test_active_goal_declared_scope_is_rechecked(domain: str, invalid: str) -> None:
    sources = Sources()
    updates: dict[str, dict[str, Any]] = {
        "privacy": {"privacy_level": PrivacyLevel.L2},
        "status": {"status": GoalStatus.CANCELLED},
        "expiry": {"expires_at": NOW},
    }
    sources.goals = [sources.goal().model_copy(update=updates[invalid])]
    assert await collect(sources, domain) == []
