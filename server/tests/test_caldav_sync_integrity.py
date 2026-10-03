"""Real parser failures flow into partial-snapshot protection at sync entry."""

from pathlib import Path

import httpx
import pytest
from test_calendar_caldav import PROPFIND_RESPONSE, SINGLE_EVENT, WEEKLY_EVENT, _report_response
from test_calendar_mirror_reminder_fence import occurrence, stats
from test_calendar_partial_sync import configured
from test_commute_query_boundary import NOW
from test_commute_reminder_fence import fixture
from test_run_cancel_fence import prepared

from app.calendar.caldav import CalDavClient, CalDavSyncService
from app.calendar.mirror import CalendarMirrorService
from app.tasks.models import TaskStatus


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("failure", ["discovery_xml", "resource_status", "invalid_recurrence"])
async def test_parser_failure_preserves_source_and_departure_reminder(
    backend: str, failure: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, planner, calendar, tasks = await fixture(storage)
        value = occurrence("caldav")
        await CalendarMirrorService(storage.database, source="caldav").apply(
            owner, "caldav:fixture", {value.ref: value}, stats=stats(), now=NOW
        )
        (before,) = await calendar.store.list_events(owner)
        await planner.plan_commute(owner, before)
        report_xml = _report_response([("fixture", SINGLE_EVENT)])
        if failure == "resource_status":
            report_xml = report_xml.replace(
                "</D:propstat>", "<D:status>HTTP/1.1 403 Forbidden</D:status></D:propstat>"
            )
        if failure == "invalid_recurrence":
            report_xml = _report_response(
                [("fixture", WEEKLY_EVENT.replace("FREQ=WEEKLY", "FREQ=UNKNOWN"))]
            )

        def response(request: httpx.Request) -> httpx.Response:
            if request.method == "PROPFIND":
                return httpx.Response(
                    207, text="invalid xml" if failure == "discovery_xml" else PROPFIND_RESPONSE
                )
            return httpx.Response(207, text=report_xml)

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
            sync = CalDavSyncService(
                storage.database,
                await configured(tmp_path, "caldav"),
                client_factory=lambda **kwargs: CalDavClient(**kwargs, client=http),
                clock=lambda: NOW,
            )
            result = await sync.sync_once()
        assert len(result.errors) == 1 and "caldav_response_invalid" in result.errors[0]
        assert result.mirrors_created == result.mirrors_updated == result.mirrors_cancelled == 0
        (after,) = await calendar.store.list_events(owner)
        assert after == before
        active = await tasks.list_tasks(owner, status=TaskStatus.ACTIVE)
        assert [task.source_ref for task in active] == [f"commute:{before.id}"]
    finally:
        await storage.close()
