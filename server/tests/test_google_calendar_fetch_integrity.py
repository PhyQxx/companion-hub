"""Only complete, bounded remote snapshots may authorize disappearance cleanup."""

from datetime import timedelta
from typing import Any

import httpx
import pytest
from test_calendar_google import NOW

from app.calendar import google
from app.calendar.google import GoogleCalendarClient, GoogleCalendarError


def event() -> dict[str, Any]:
    return {
        "id": "fixture-event",
        "start": {"dateTime": NOW.isoformat()},
        "end": {"dateTime": (NOW + timedelta(hours=1)).isoformat()},
    }


async def fetch(payloads: list[Any]) -> tuple[list[Any], int]:
    calls = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "fixture", "expires_in": 3600})
        calls += 1
        if calls > len(payloads):
            raise httpx.ReadError("synthetic request cap", request=request)
        value = payloads[calls - 1]
        if isinstance(value, bytes):
            return httpx.Response(200, content=value)
        return httpx.Response(200, json=value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = GoogleCalendarClient(
            client_id="fixture", client_secret="fixture", refresh_token="fixture", client=http
        )
        result = await client.list_events("fixture", start=NOW, end=NOW + timedelta(days=1))
        return result, calls


@pytest.mark.parametrize(
    "payload",
    [[], None, {"items": None}, {"items": {}}, {"items": [None]}, b"invalid json"],
)
async def test_invalid_snapshot_is_not_an_empty_complete_calendar(payload: Any) -> None:
    with pytest.raises(GoogleCalendarError) as error:
        await fetch([payload])
    assert error.value.reason_code == "google_response_invalid"


@pytest.mark.parametrize(
    "patch",
    [
        {"id": None},
        {"id": ""},
        {"start": None},
        {"start": "invalid"},
        {"start": {"dateTime": "invalid"}},
        {"end": None},
        {"end": {"dateTime": "invalid"}},
        {"end": {"dateTime": NOW.isoformat()}},
    ],
)
async def test_invalid_active_event_invalidates_whole_snapshot(patch: dict[str, Any]) -> None:
    value = event() | patch
    with pytest.raises(GoogleCalendarError) as error:
        await fetch([{"items": [event(), value]}])
    assert error.value.reason_code == "google_event_invalid"


@pytest.mark.parametrize("page_token", [[], {}, 7, True, ""])
async def test_invalid_pagination_token_rejects_partial_results(page_token: Any) -> None:
    with pytest.raises(GoogleCalendarError) as error:
        await fetch([{"items": [event()], "nextPageToken": page_token}])
    assert error.value.reason_code == "google_response_invalid"


async def test_repeated_page_token_stops_without_repeating_requests() -> None:
    with pytest.raises(GoogleCalendarError) as error:
        await fetch([{"nextPageToken": "same"}, {"nextPageToken": "same"}])
    assert error.value.reason_code == "google_page_cycle"


async def test_page_budget_exhaustion_rejects_partial_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(google, "MAX_EVENT_PAGES", 2, raising=False)
    with pytest.raises(GoogleCalendarError) as error:
        await fetch([{"nextPageToken": "one"}, {"nextPageToken": "two"}])
    assert error.value.reason_code == "google_page_limit"


async def test_transport_failure_is_reported_without_partial_success() -> None:
    with pytest.raises(GoogleCalendarError) as error:
        await fetch([{"items": [event()], "nextPageToken": "one"}])
    assert error.value.reason_code == "google_unreachable"


async def test_last_allowed_page_and_empty_cancelled_tombstone_are_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(google, "MAX_EVENT_PAGES", 2, raising=False)
    values, calls = await fetch(
        [
            {"items": [event()], "nextPageToken": "one"},
            {"items": [{"id": "old", "status": "cancelled"}]},
        ]
    )
    assert calls == 2 and len(values) == 1 and values[0].ref == "google:fixture-event"
