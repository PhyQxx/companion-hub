"""Commute policies reject unowned/inactive DTOs before any connector work."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from app.calendar.models import CalendarEventView
from app.commute.service import CommuteRouteError, CommuteService
from app.tasks.models import TaskView

NOW = datetime(2026, 10, 3, tzinfo=UTC)


class Calendar:
    def __init__(self) -> None:
        self.owner = uuid4()
        self.event = CalendarEventView(
            id=uuid4(),
            user_id=self.owner,
            calendar_id="primary",
            title="Synthetic outing",
            starts_at=NOW + timedelta(hours=2),
            ends_at=NOW + timedelta(hours=3),
            location="Synthetic destination",
            status="active",
        )
        self.events = [self.event]
        self.calls: list[dict[str, Any]] = []

    async def list_events(self, user_id: object, **kwargs: Any) -> list[CalendarEventView]:
        self.calls.append(kwargs)
        return self.events


class Tasks:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def cancel_tasks_by_source_ref(self, user_id: object, source_ref: str) -> int:
        self.calls.append({"cancel": source_ref})
        return 0

    async def create(self, **kwargs: Any) -> TaskView:
        self.calls.append(kwargs)
        return TaskView(
            id=uuid4(), status="active", **{k: v for k, v in kwargs.items() if k != "now"}
        )


class Provider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def geocode(self, address: str, *, city: str | None = None) -> dict[str, object]:
        self.calls.append(address)
        return {"location": "120,30"}

    async def route(self, origin: str, destination: str, **kwargs: Any) -> dict[str, object]:
        self.calls.append("route")
        return {"route": {"paths": [{"distance": "100", "duration": "60"}]}}


def service(calendar: Calendar, tasks: Tasks, provider: Provider) -> CommuteService:
    return CommuteService(calendar, tasks, provider, origin="Synthetic origin", clock=lambda: NOW)


@pytest.mark.parametrize(
    "change",
    [
        {"user_id": uuid4()},
        {"status": "cancelled"},
        {"starts_at": NOW - timedelta(seconds=1)},
        {"starts_at": NOW + timedelta(hours=25)},
        {"location": "  "},
    ],
)
async def test_next_outing_rechecks_repository_scope(change: dict[str, Any]) -> None:
    calendar, tasks, provider = Calendar(), Tasks(), Provider()
    calendar.events = [calendar.event.model_copy(update=change)]
    assert await service(calendar, tasks, provider).next_outing(calendar.owner) is None
    assert not tasks.calls
    assert not provider.calls


@pytest.mark.parametrize(
    "change",
    [
        {"user_id": uuid4()},
        {"status": "cancelled"},
        {"location": "  "},
    ],
)
async def test_invalid_direct_event_does_not_call_provider_or_tasks(change: dict[str, Any]) -> None:
    calendar, tasks, provider = Calendar(), Tasks(), Provider()
    with pytest.raises(CommuteRouteError):
        await service(calendar, tasks, provider).plan_commute(
            calendar.owner,
            calendar.event.model_copy(update=change),
        )
    assert not provider.calls
    assert not tasks.calls


@pytest.mark.parametrize("change", ["origin_city", "destination_address"])
async def test_provider_retained_payloads_do_not_change_the_accepted_route(change: str) -> None:
    calendar, tasks = Calendar(), Tasks()

    class RetainingProvider(Provider):
        def __init__(self) -> None:
            super().__init__()
            self.origin: dict[str, object] = {"location": "120,30", "citycode": "original"}
            self.destination: dict[str, object] = {
                "location": "121,31",
                "citycode": "destination",
                "adcode": "fixture",
                "formatted_address": "original destination",
            }
            self.origin_city: object = None

        async def geocode(self, address: str, *, city: str | None = None) -> dict[str, object]:
            if address == "Synthetic origin":
                return self.origin
            if change == "origin_city":
                self.origin["citycode"] = "changed"
            return self.destination

        async def route(self, origin: str, destination: str, **kwargs: Any) -> dict[str, object]:
            self.origin_city = kwargs["origin_citycode"]
            return await super().route(origin, destination, **kwargs)

        async def weather(self, adcode: str, *, extensions: str) -> dict[str, object]:
            if change == "destination_address":
                self.destination["formatted_address"] = "changed destination"
            return {"lives": [{"weather": "fixture", "temperature": "20"}]}

    provider = RetainingProvider()
    result = await service(calendar, tasks, provider).plan_commute(
        calendar.owner,
        calendar.event,
        reminder=False,
    )
    assert provider.origin_city == "original"
    assert result.destination_text == "original destination"
    assert tasks.calls == []


async def test_next_outing_orders_detached_rows_by_start_time() -> None:
    calendar, tasks, provider = Calendar(), Tasks(), Provider()
    later = calendar.event.model_copy(update={"starts_at": NOW + timedelta(hours=4)})
    calendar.events = [later, calendar.event]
    selected = await service(calendar, tasks, provider).next_outing(calendar.owner)
    assert selected is not None and selected.starts_at == calendar.event.starts_at


async def test_selected_outing_owns_nested_participants() -> None:
    from app.calendar.models import CalendarParticipant

    calendar, tasks, provider = Calendar(), Tasks(), Provider()
    calendar.event.participants.append(CalendarParticipant(name="Synthetic participant"))
    selected = await service(calendar, tasks, provider).next_outing(calendar.owner)
    assert selected is not None
    calendar.event.participants.clear()
    assert len(selected.participants) == 1
