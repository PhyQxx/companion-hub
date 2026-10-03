"""CalDAV parse failures and finite expansion cannot authorize false absence."""

from datetime import timedelta

import httpx
import pytest
from test_calendar_caldav import (
    NOW,
    PROPFIND_RESPONSE,
    SINGLE_EVENT,
    WEEKLY_EVENT,
    _report_response,
)

from app.calendar import caldav
from app.calendar.caldav import CalDavClient, CalDavError


async def fetch(body: str, *, discover: bool = False) -> object:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(207, text=body))
    ) as http:
        client = CalDavClient(
            base_url="https://fixture.invalid", username="fixture", secret="fixture", client=http
        )
        if discover:
            return await client.list_calendars()
        return await client.fetch_window("/calendar", start=NOW, end=NOW + timedelta(days=10))


@pytest.mark.parametrize(
    "body",
    [
        "invalid xml",
        "<empty/>",
        _report_response([("fixture", SINGLE_EVENT)])
        .replace("<D:href>", "<D:missing>")
        .replace("</D:href>", "</D:missing>"),
        _report_response([("fixture", SINGLE_EVENT)])
        .replace("<C:calendar-data>", "<C:missing>")
        .replace("</C:calendar-data>", "</C:missing>"),
        _report_response([("fixture", "invalid ical")]),
        _report_response([("fixture", "BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR")]),
        _report_response([("fixture", SINGLE_EVENT)]).replace(
            "</D:propstat>", "<D:status>HTTP/1.1 403 Forbidden</D:status></D:propstat>"
        ),
        _report_response([("fixture", SINGLE_EVENT)]).replace(
            "</D:response>", "<D:status>HTTP/1.1 404 Not Found</D:status></D:response>"
        ),
    ],
)
async def test_bad_report_is_not_a_complete_empty_snapshot(body: str) -> None:
    with pytest.raises(CalDavError) as error:
        await fetch(body)
    assert error.value.reason_code == "caldav_response_invalid"


@pytest.mark.parametrize(
    "body",
    [
        "invalid xml",
        "<empty/>",
        PROPFIND_RESPONSE.replace(
            "</D:propstat>", "<D:status>HTTP/1.1 403 Forbidden</D:status></D:propstat>"
        ),
    ],
)
async def test_failed_discovery_does_not_authorize_removal(body: str) -> None:
    with pytest.raises(CalDavError) as error:
        await fetch(body, discover=True)
    assert error.value.reason_code == "caldav_response_invalid"


@pytest.mark.parametrize(
    "ical",
    [
        SINGLE_EVENT.replace("DTSTART:20260920T090000Z\n", ""),
        SINGLE_EVENT.replace("DTEND:20260920T100000Z", "DTEND:20260920T080000Z"),
        WEEKLY_EVENT.replace("FREQ=WEEKLY", "FREQ=UNKNOWN"),
    ],
)
async def test_bad_event_or_rule_fails_whole_report(ical: str) -> None:
    with pytest.raises(CalDavError) as error:
        await fetch(_report_response([("good", SINGLE_EVENT), ("bad", ical)]))
    assert error.value.reason_code == "caldav_response_invalid"


async def test_recurrence_expansion_cap_rejects_partial_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(caldav, "MAX_RECURRENCE_STEPS", 3, raising=False)
    ical = SINGLE_EVENT.replace("END:VEVENT", "RRULE:FREQ=DAILY\nEND:VEVENT")
    with pytest.raises(CalDavError) as error:
        await fetch(_report_response([("fixture", ical)]))
    assert error.value.reason_code == "caldav_recurrence_limit"


async def test_report_resource_cap_rejects_truncated_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(caldav, "MAX_CALENDAR_RESOURCES", 1, raising=False)
    with pytest.raises(CalDavError) as error:
        await fetch(_report_response([("one", SINGLE_EVENT), ("two", SINGLE_EVENT)]))
    assert error.value.reason_code == "caldav_resource_limit"


async def test_report_occurrence_cap_rejects_partial_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(caldav, "MAX_CALENDAR_OCCURRENCES", 1, raising=False)
    with pytest.raises(CalDavError) as error:
        await fetch(_report_response([("one", SINGLE_EVENT), ("two", SINGLE_EVENT)]))
    assert error.value.reason_code == "caldav_occurrence_limit"


async def test_parser_byte_cap_is_not_silent_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(caldav, "MAX_CALENDAR_RESPONSE_BYTES", 8, raising=False)
    with pytest.raises(CalDavError) as error:
        await fetch(_report_response([("one", SINGLE_EVENT)]))
    assert error.value.reason_code == "caldav_response_limit"


async def test_last_allowed_recurrence_and_successful_property_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(caldav, "MAX_RECURRENCE_STEPS", 3, raising=False)
    ical = SINGLE_EVENT.replace("END:VEVENT", "RRULE:FREQ=DAILY;COUNT=3\nEND:VEVENT")
    body = _report_response([("fixture", ical)]).replace(
        "</D:propstat>", "<D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
    )
    values = await fetch(body)
    assert isinstance(values, list) and len(values) == 3


@pytest.mark.parametrize(
    "anchor",
    ["RECURRENCE-ID:20260920T090000Z", "RECURRENCE-ID;TZID=Asia/Shanghai:20260920T170000"],
)
async def test_shifted_override_keeps_anchor_but_uses_actual_time(anchor: str) -> None:
    master = SINGLE_EVENT.replace("END:VEVENT", "RRULE:FREQ=DAILY;COUNT=2\nEND:VEVENT")
    override = f"""BEGIN:VEVENT
UID:single-1
{anchor}
DTSTART:20260920T120000Z
DTEND:20260920T130000Z
SUMMARY:shifted instance
END:VEVENT
"""
    values = await fetch(
        _report_response([("fixture", master.replace("END:VCALENDAR", override + "END:VCALENDAR"))])
    )
    assert isinstance(values, list) and len(values) == 2
    assert len({value.ref for value in values}) == 2
    shifted = next(value for value in values if value.summary == "shifted instance")
    assert shifted.ref == "caldav:single-1#20260920T170000"
    assert shifted.starts_at.hour == 12 and shifted.ends_at.hour == 13


async def test_all_day_end_date_preserves_full_duration() -> None:
    ical = SINGLE_EVENT.replace("DTSTART:20260920T090000Z", "DTSTART;VALUE=DATE:20260920").replace(
        "DTEND:20260920T100000Z", "DTEND;VALUE=DATE:20260923"
    )
    values = await fetch(_report_response([("fixture", ical)]))
    assert isinstance(values, list) and len(values) == 1
    assert values[0].all_day and values[0].ends_at - values[0].starts_at == timedelta(days=3)


async def test_invalid_all_day_end_date_fails_instead_of_becoming_one_day() -> None:
    ical = SINGLE_EVENT.replace("DTSTART:20260920T090000Z", "DTSTART;VALUE=DATE:20260920").replace(
        "DTEND:20260920T100000Z", "DTEND;VALUE=DATE:20260919"
    )
    with pytest.raises(CalDavError) as error:
        await fetch(_report_response([("fixture", ical)]))
    assert error.value.reason_code == "caldav_response_invalid"


async def test_scan_budget_includes_instances_before_query_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(caldav, "MAX_RECURRENCE_STEPS", 3, raising=False)
    ical = SINGLE_EVENT.replace("20260920", "20260901").replace(
        "END:VEVENT", "RRULE:FREQ=DAILY\nEND:VEVENT"
    )
    with pytest.raises(CalDavError) as error:
        await fetch(_report_response([("fixture", ical)]))
    assert error.value.reason_code == "caldav_recurrence_limit"


@pytest.mark.parametrize(
    "ical",
    [
        SINGLE_EVENT.replace("UID:single-1\n", ""),
        SINGLE_EVENT.replace("UID:single-1", "UID:" + "x" * 180),
        SINGLE_EVENT.replace(
            "END:VCALENDAR",
            SINGLE_EVENT.split("BEGIN:VEVENT", 1)[1]
            .split("END:VEVENT", 1)[0]
            .replace("UID:single-1", "BEGIN:VEVENT\nUID:another-uid")
            + "END:VEVENT\nEND:VCALENDAR",
        ),
    ],
)
async def test_unaddressable_or_mixed_uid_resource_is_not_partially_accepted(ical: str) -> None:
    with pytest.raises(CalDavError) as error:
        await fetch(_report_response([("fixture", ical)]))
    assert error.value.reason_code == "caldav_response_invalid"


async def test_failed_optional_etag_does_not_hide_successful_calendar_data() -> None:
    body = (
        _report_response([("fixture", SINGLE_EVENT)])
        .replace('<D:getetag>"fixture"</D:getetag>', "")
        .replace(
            "</D:response>",
            '<D:propstat><D:prop><D:getetag>"invalid"</D:getetag></D:prop>'
            "<D:status>HTTP/1.1 404 Not Found</D:status></D:propstat></D:response>",
        )
    )
    values = await fetch(body)
    assert isinstance(values, list) and len(values) == 1
    assert values[0].etag == "/calendars/personal/fixture.ics"


def shifted_component() -> str:
    return """BEGIN:VEVENT
UID:single-1
RECURRENCE-ID:20260920T090000Z
DTSTART:20260920T120000Z
DTEND:20260920T130000Z
SUMMARY:shifted instance
END:VEVENT
"""


async def test_override_before_master_has_same_instances() -> None:
    master = SINGLE_EVENT.replace("END:VEVENT", "RRULE:FREQ=DAILY;COUNT=2\nEND:VEVENT")
    body = master.replace("BEGIN:VEVENT", shifted_component() + "BEGIN:VEVENT", 1)
    values = await fetch(_report_response([("fixture", body)]))
    assert isinstance(values, list) and len(values) == 2
    assert len({value.ref for value in values}) == 2
    shifted = next(value for value in values if value.summary == "shifted instance")
    assert shifted.starts_at.hour == 12 and shifted.ref == "caldav:single-1#20260920T170000"


async def test_standalone_override_has_stable_original_anchor() -> None:
    body = "BEGIN:VCALENDAR\nVERSION:2.0\n" + shifted_component() + "END:VCALENDAR"
    values = await fetch(_report_response([("fixture", body)]))
    assert isinstance(values, list) and len(values) == 1
    assert values[0].starts_at.hour == 12 and values[0].ref == "caldav:single-1#20260920T170000"


async def test_two_masters_of_same_uid_cannot_be_partially_accepted() -> None:
    body = SINGLE_EVENT.replace("END:VCALENDAR", SINGLE_EVENT[SINGLE_EVENT.index("BEGIN:VEVENT") :])
    with pytest.raises(CalDavError) as error:
        await fetch(_report_response([("fixture", body)]))
    assert error.value.reason_code == "caldav_response_invalid"
