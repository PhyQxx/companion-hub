"""L1 daily reports cannot disclose private meeting-derived task titles."""

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from test_commute_query_boundary import NOW
from test_run_cancel_fence import prepared

from app.cognition.store import CognitiveStore
from app.db import AppUserRecord, TaskItemRecord
from app.schemas import PrivacyLevel
from app.tasks.brief import BriefView, DailyBriefService
from app.tasks.models import TaskKind, TaskStatus, TaskTrigger
from app.tasks.review import DailyReviewService
from app.tasks.store import TaskStore


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize(
    "domain,timing",
    [("brief", "today"), ("review", "today"), ("review", "tomorrow"), ("review", "completed")],
)
@pytest.mark.parametrize("privacy", [PrivacyLevel.L2])
async def test_daily_report_omits_private_task_titles_and_sources(
    backend: str, domain: str, privacy: PrivacyLevel, timing: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner = uuid4()
        async with storage.database.sessions.begin() as session:
            session.add(AppUserRecord(id=owner, display_name="Fixture", status="active"))
        tasks = TaskStore(storage.database)
        trigger_at = NOW + timedelta(days=1 if timing == "tomorrow" else 0, hours=2)
        created = await tasks.create(
            user_id=owner,
            kind=TaskKind.REMINDER,
            title="private meeting-derived title",
            trigger=TaskTrigger(type="time", at=trigger_at),
            privacy_level=privacy,
            source="meeting",
            source_ref="meeting:fixture:action",
            now=NOW,
        )
        if timing == "completed":
            await tasks.complete_task(owner, created.id, now=NOW + timedelta(hours=1))
        service = (DailyBriefService if domain == "brief" else DailyReviewService)(
            storage.database, tasks, CognitiveStore(storage.database), clock=lambda: NOW
        )
        report = await service.build(owner)
        assert "private meeting-derived title" not in report.text
        entries = report.facts if isinstance(report, BriefView) else report.items
        assert f"task:{created.id}" not in {value.source for value in entries}
        assert (await tasks.get_task(owner, created.id)).privacy_level == privacy
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("domain", ["brief", "review"])
async def test_private_rows_do_not_hide_public_facts_before_page_limit(
    backend: str, domain: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner = uuid4()
        async with storage.database.sessions.begin() as session:
            session.add(AppUserRecord(id=owner, display_name="Fixture", status="active"))
        tasks = TaskStore(storage.database)
        public = await tasks.create(
            user_id=owner,
            kind=TaskKind.REMINDER,
            title="visible public fixture",
            trigger=TaskTrigger(type="time", at=NOW + timedelta(hours=2)),
            now=NOW,
        )
        async with storage.database.sessions.begin() as session:
            session.add_all(
                [
                    TaskItemRecord(
                        id=uuid4(),
                        user_id=owner,
                        kind="reminder",
                        title="private newer fixture",
                        status="active",
                        trigger_type="time",
                        trigger_config={
                            "type": "time",
                            "at": (NOW + timedelta(hours=3)).isoformat(),
                        },
                        source="meeting",
                        source_ref=f"fixture:{index}",
                        next_fire_at=NOW + timedelta(hours=3),
                        fire_count=0,
                        privacy_level="L2",
                        created_at=NOW + timedelta(seconds=index + 1),
                        updated_at=NOW,
                    )
                    for index in range(205)
                ]
            )
        service = (DailyBriefService if domain == "brief" else DailyReviewService)(
            storage.database, tasks, CognitiveStore(storage.database), clock=lambda: NOW
        )
        report = await service.build(owner)
        assert (
            "visible public fixture" in report.text and "private newer fixture" not in report.text
        )
        entries = report.facts if isinstance(report, BriefView) else report.items
        assert [value.source for value in entries] == [f"task:{public.id}"]
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("privacy", list(PrivacyLevel))
async def test_owned_task_queries_apply_explicit_disclosure_scope(
    backend: str, privacy: PrivacyLevel, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, foreign = uuid4(), uuid4()
        async with storage.database.sessions.begin() as session:
            session.add_all(
                [
                    AppUserRecord(id=value, display_name="Fixture", status="active")
                    for value in (owner, foreign)
                ]
            )
        tasks = TaskStore(storage.database)
        for level in (PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2):
            for user in (owner, foreign):
                await tasks.create(
                    user_id=user,
                    kind=TaskKind.REMINDER,
                    title=str(level),
                    trigger=TaskTrigger(type="time", at=NOW + timedelta(hours=1)),
                    privacy_level=level,
                    now=NOW,
                )
        views = await tasks.list_tasks(owner, max_privacy_level=privacy, status=TaskStatus.ACTIVE)
        expected = (
            []
            if privacy == PrivacyLevel.L3
            else [f"L{value}" for value in range(int(str(privacy)[1]) + 1)]
        )
        assert sorted(view.title for view in views) == expected
        assert all(view.user_id == owner for view in views)
        assert len(await tasks.list_tasks(owner)) == 3
    finally:
        await storage.close()
