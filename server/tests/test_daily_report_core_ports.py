"""Standalone collectors and legacy projections retain the same public contracts."""

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from test_commute_query_boundary import NOW
from test_daily_report_collection_boundary import Sources, collect
from test_run_cancel_fence import prepared

from app.calendar.store import CalendarStore
from app.cognition.store import CognitiveStore
from app.db import AppUserRecord
from app.tasks import brief, brief_core, brief_models, review, review_core, review_models
from app.tasks.brief_core import DailyBriefCollector
from app.tasks.brief_models import BriefCommute
from app.tasks.models import TaskKind, TaskTrigger
from app.tasks.review_core import DailyReviewCollector
from app.tasks.store import TaskStore
from scripts.check_architecture import allowed


@pytest.mark.parametrize("domain", ["brief", "review"])
async def test_standalone_collector_uses_owned_ports_without_database(domain: str) -> None:
    sources = Sources()
    sources.tasks = [sources.task()]
    if domain == "brief":
        core = DailyBriefCollector(
            sources,
            sources,
            calendar_store=sources,
            contact_store=sources,
            weather_fetcher=sources.weather,
            commute_fetcher=sources.commute,
            clock=lambda: NOW,
        )
        result = await core.collect_facts(sources.owner, brief_date=NOW.date())
    else:
        review_collector = DailyReviewCollector(
            sources,
            sources,
            calendar_store=sources,
            clock=lambda: NOW,
        )
        review_result = await review_collector.collect_items(sources.owner, review_date=NOW.date())
        assert len(review_result) == 1 and review_result[0].source == f"task:{sources.tasks[0].id}"
        return
    assert len(result) == 1 and result[0].source == f"task:{sources.tasks[0].id}"


@pytest.mark.parametrize("domain", ["brief", "review"])
async def test_repository_resolved_inherited_goal_privacy_remains_visible(domain: str) -> None:
    sources = Sources()
    sources.goals = [sources.goal().model_copy(update={"privacy_level": None})]
    entries = await collect(sources, domain)
    assert [entry.source for entry in entries] == [f"goal:{sources.goals[0].id}"]


@pytest.mark.parametrize("domain", ["brief", "review"])
async def test_utc_naive_calendar_projection_is_rendered_consistently(domain: str) -> None:
    sources = Sources()
    event = sources.event(domain)
    sources.events = [
        event.model_copy(
            update={
                "starts_at": event.starts_at.replace(tzinfo=None),
                "ends_at": event.ends_at.replace(tzinfo=None),
            }
        )
    ]
    entries = await collect(sources, domain)
    assert len(entries) == 1 and "09:00" in entries[0].text
    assert sources.events[0].starts_at.tzinfo is None


@pytest.mark.parametrize("scope", ["owned", "foreign", "other_day"])
async def test_commute_fact_uses_declared_calendar_identity_and_scope(scope: str) -> None:
    sources = Sources()
    event = sources.event("brief")
    sources.commute_value = BriefCommute(
        "fixture destination",
        NOW,
        "fixture event",
        NOW + timedelta(days=scope == "other_day"),
        "walking",
        15,
        event_id=event.id,
        user_id=uuid4() if scope == "foreign" else sources.owner,
    )
    entries = await collect(sources, "brief")
    assert [entry.source for entry in entries] == (
        [f"calendar:{event.id}"] if scope == "owned" else []
    )


def test_legacy_model_and_helper_exports_retain_object_identity() -> None:
    for name in (
        "BriefFact",
        "BriefView",
        "BriefWeather",
        "BriefCommute",
        "BriefWeatherFetcher",
        "BriefDeliverer",
    ):
        assert getattr(brief, name) is getattr(brief_models, name)
    for name in ("ReviewItem", "ReviewView", "ReviewSection", "ReviewDeliverer", "_SECTION_TITLES"):
        assert getattr(review, name) is getattr(review_models, name)
    assert brief.compose_brief_text is brief_core.compose_brief_text
    assert brief._aware is brief_core._aware
    assert review.compose_review_text is review_core.compose_review_text
    assert review._aware is review_core._aware
    assert review._cap_sections is review_core._cap_sections


@pytest.mark.parametrize(
    "module",
    [
        "app.tasks.brief_core",
        "app.tasks.review_core",
        "app.tasks.brief_models",
        "app.tasks.review_models",
        "app.tasks.report_ports",
        "app.tasks.report_rules",
        "app.contacts.models",
    ],
)
def test_report_core_static_boundary_rejects_adapters(module: str) -> None:
    for target in (
        "sqlalchemy",
        "httpx",
        "openai",
        "app.db",
        "app.tasks.store",
        "app.cognition.store",
        "app.contacts.store",
        "app.calendar.store",
        "app.wiring",
    ):
        assert not allowed(module, target)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("domain", ["brief", "review"])
@pytest.mark.parametrize("source", ["task", "calendar"])
async def test_long_persistent_titles_build_report_without_modifying_source(
    backend: str,
    domain: str,
    source: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, title = uuid4(), "长" * 320
        async with storage.database.sessions.begin() as session:
            session.add(AppUserRecord(id=owner, display_name="fixture", status="active"))
        tasks, calendar = TaskStore(storage.database), CalendarStore(storage.database)
        if source == "task":
            task = await tasks.create(
                user_id=owner,
                kind=TaskKind.REMINDER,
                title=title,
                trigger=TaskTrigger(type="time", at=NOW + timedelta(hours=1)),
                now=NOW,
            )
            identifier = task.id
        else:
            starts_at = NOW + timedelta(days=domain == "review", hours=1)
            event = await calendar.create_event(
                user_id=owner,
                title=title,
                starts_at=starts_at,
                ends_at=starts_at + timedelta(hours=1),
                location="地" * 240,
                now=NOW,
            )
            identifier = event.id
        service = (brief.DailyBriefService if domain == "brief" else review.DailyReviewService)(
            storage.database,
            tasks,
            CognitiveStore(storage.database),
            calendar_store=calendar,
            clock=lambda: NOW,
        )
        report = await service.build(owner)
        entries = report.facts if isinstance(report, brief.BriefView) else report.items
        assert len(entries) == 1 and entries[0].text.endswith("…")
        assert entries[0].source == f"{source}:{identifier}"
        assert title not in report.text
        if source == "task":
            assert (await tasks.get_task(owner, identifier)).title == title
        else:
            assert (await calendar.get_event(owner, identifier)).title == title
        assert (await service.build(owner)).id == report.id
    finally:
        await storage.close()
