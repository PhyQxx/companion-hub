"""Detached commute ports propagate interrupts without duplicate connector work."""

import asyncio
from typing import Any

import pytest
from test_commute_query_boundary import Calendar, Provider, Tasks, service

from app.commute.ports import CommuteAmapProvider, CommuteCalendarStore
from app.commute.service import CommuteAmapProvider as LegacyProvider
from app.commute.service import CommuteCalendarStore as LegacyCalendar
from app.commute.service import CommuteRouteError
from app.harness.budget import BudgetDenied
from app.tools.amap import AmapProviderError as LegacyError
from app.tools.amap_models import AmapProviderError
from app.tools.nearby import parse_route as LegacyParser
from app.tools.route_parser import parse_route
from scripts.check_architecture import allowed


@pytest.mark.parametrize("stage", ["origin", "destination", "route", "weather"])
@pytest.mark.parametrize("failure", ["ordinary", "cancel", "budget"])
async def test_connector_failure_or_cancellation_is_not_retried(stage: str, failure: str) -> None:
    class Broken(Provider):
        def fail(self, current: str) -> None:
            self.calls.append(current)
            if current == stage:
                if failure == "cancel":
                    raise asyncio.CancelledError
                if failure == "budget":
                    raise BudgetDenied("synthetic_budget_denied")
                raise RuntimeError("synthetic connector failure")

        async def geocode(self, address: str, *, city: str | None = None) -> dict[str, object]:
            self.fail("origin" if address == "Synthetic origin" else "destination")
            return {"location": "120,30", "adcode": "fixture"}

        async def route(self, origin: str, destination: str, **kwargs: Any) -> dict[str, object]:
            self.fail("route")
            return {"route": {"paths": [{"distance": "100", "duration": "60"}]}}

        async def weather(self, adcode: str, *, extensions: str) -> dict[str, object]:
            self.fail("weather")
            return {}

    calendar, tasks, provider = Calendar(), Tasks(), Broken()
    if stage == "weather" and failure == "ordinary":
        result = await service(calendar, tasks, provider).plan_commute(
            calendar.owner,
            calendar.event,
            reminder=False,
        )
        assert result.weather_summary is None
        assert result.duration_s == 60
    else:
        with pytest.raises(
            asyncio.CancelledError
            if failure == "cancel"
            else BudgetDenied
            if failure == "budget"
            else CommuteRouteError
        ):
            await service(calendar, tasks, provider).plan_commute(
                calendar.owner,
                calendar.event,
                reminder=False,
            )
    assert (
        provider.calls
        == ["origin", "destination", "route", "weather"][
            : ["origin", "destination", "route", "weather"].index(stage) + 1
        ]
    )
    assert not tasks.calls


def test_legacy_port_error_and_route_exports_remain_identical() -> None:
    assert LegacyProvider is CommuteAmapProvider
    assert LegacyCalendar is CommuteCalendarStore
    assert LegacyError is AmapProviderError
    assert LegacyParser is parse_route
    error = AmapProviderError("fixture", candidates=[{"value": "synthetic"}])
    assert error.reason_code == "fixture" and error.candidates == [{"value": "synthetic"}]
    with pytest.raises(LegacyError, match="route_unavailable"):
        parse_route({}, "driving")


@pytest.mark.parametrize(
    "module",
    [
        "app.commute.service",
        "app.commute.ports",
        "app.calendar.models",
        "app.tasks.models",
        "app.tools.amap_models",
        "app.tools.route_parser",
    ],
)
@pytest.mark.parametrize(
    "dependency", ["app.db", "app.tasks.store", "sqlalchemy", "httpx", "openai"]
)
def test_detached_commute_static_boundaries(module: str, dependency: str) -> None:
    assert not allowed(module, dependency)
