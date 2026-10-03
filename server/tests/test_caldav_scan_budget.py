"""The whole calendar owns one recurrence scan allowance per fetch."""

import asyncio
from datetime import timedelta

import httpx
import pytest
from test_caldav_fetch_integrity import fetch
from test_calendar_caldav import NOW, SINGLE_EVENT, _report_response

from app.calendar import caldav
from app.calendar.caldav import CalDavClient, CalDavError


def recurring(uid: str, count: int) -> str:
    return SINGLE_EVENT.replace("UID:single-1", f"UID:{uid}").replace(
        "END:VEVENT", f"RRULE:FREQ=DAILY;COUNT={count}\nEND:VEVENT"
    )


async def test_calendar_scan_allowance_is_shared_by_all_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(caldav, "MAX_CALENDAR_RECURRENCE_STEPS", 3, raising=False)
    with pytest.raises(CalDavError) as error:
        await fetch(_report_response([("one", recurring("one", 2)), ("two", recurring("two", 2))]))
    assert error.value.reason_code == "caldav_recurrence_limit"


async def test_last_allowed_shared_candidate_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(caldav, "MAX_CALENDAR_RECURRENCE_STEPS", 3, raising=False)
    values = await fetch(
        _report_response([("one", recurring("one", 2)), ("two", recurring("two", 1))])
    )
    assert isinstance(values, list) and len(values) == 3


async def test_scan_allowance_is_independent_for_concurrent_fetches_on_one_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(caldav, "MAX_CALENDAR_RECURRENCE_STEPS", 3, raising=False)
    body = _report_response([("one", recurring("one", 2))])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(207, text=body))
    ) as http:
        client = CalDavClient(
            base_url="https://fixture.invalid", username="fixture", secret="fixture", client=http
        )
        results = await asyncio.gather(
            client.fetch_window("/one", start=NOW, end=NOW + timedelta(days=10)),
            client.fetch_window("/two", start=NOW, end=NOW + timedelta(days=10)),
        )
        assert [len(values) for values in results] == [2, 2]
        assert len(await client.fetch_window("/one", start=NOW, end=NOW + timedelta(days=10))) == 2
