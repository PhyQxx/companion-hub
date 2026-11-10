from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db import ActivitySummaryRecord, AppUserRecord, Base, create_database
from app.ids import uuid7
from app.schemas.common import PrivacyLevel
from app.tasks.activity_summary import ActivitySummaryService
from app.tasks.activity_summary_scheduler import ActivitySummaryScheduler
from app.timeline.models import TimelineActor, TimelineSourceType
from app.timeline.store import TimelineStore


@pytest.mark.asyncio
async def test_periods_privacy_idempotency_and_persistence(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'reports.db'}"
    db = create_database(url)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    owner, other = uuid7(), uuid7()
    async with db.sessions.begin() as session:
        session.add_all(
            [AppUserRecord(id=u, display_name="User", status="active") for u in (owner, other)]
        )
    timeline = TimelineStore(db)
    for user, privacy, text in [
        (owner, PrivacyLevel.L1, "整理设计"),
        (owner, PrivacyLevel.L2, "私密内容"),
        (other, PrivacyLevel.L1, "其他用户"),
    ]:
        await timeline.index_custom(
            user_id=user,
            source_type=TimelineSourceType.SYSTEM,
            source_id=str(uuid7()),
            actor=TimelineActor.USER,
            event_type="browser.visited",
            summary=text,
            title="活动",
            privacy_level=privacy,
            occurred_at=datetime(2026, 11, 8, 16, 0, tzinfo=UTC),
        )
    service = ActivitySummaryService(db)
    now = datetime(2026, 11, 9, 16, 1, tzinfo=UTC)
    result = await service.build(owner, "daily", now=now)
    assert result.period_start == date(2026, 11, 9)
    assert "整理设计" in result.text
    assert "私密内容" not in result.text and "其他用户" not in result.text
    assert (await service.build(owner, "daily", now=now)).id == result.id
    weekly = await service.build(owner, "weekly", now=now)
    assert (weekly.period_start, weekly.period_end) == (date(2026, 11, 2), date(2026, 11, 9))
    assert "整理设计" not in weekly.text
    await ActivitySummaryScheduler(service).run_once(now=now)
    async with db.sessions() as session:
        assert len(list(await session.scalars(select(ActivitySummaryRecord)))) == 4
    await db.close()
    db = create_database(url)
    service = ActivitySummaryService(db)
    assert len(await service.recent(owner)) == 2
    await service.remove(other, result.id)
    assert len(await service.recent(owner)) == 2
    await service.remove(owner, result.id)
    assert len(await service.recent(owner)) == 1
    await db.close()


async def test_summary_api(tmp_path: Path) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api.activity_summaries import create_activity_summaries_router
    from app.auth import AuthService

    db = create_database(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        auth = AuthService(db)
        owner = await auth.setup(display_name="Owner", password="correct horse")
        app = FastAPI()
        app.include_router(create_activity_summaries_router(ActivitySummaryService(db), auth))
        headers = {"Authorization": f"Bearer {owner.access_token}"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/v1/activity-summaries")).status_code == 401
            response = await client.post(
                "/api/v1/activity-summaries/generate?kind=weekly", headers=headers
            )
            assert response.status_code == 200
            assert (
                await client.get("/api/v1/activity-summaries?kind=weekly", headers=headers)
            ).json()[0]["id"] == response.json()["id"]
            assert (
                await client.get("/api/v1/activity-summaries?limit=101", headers=headers)
            ).status_code == 422
            assert (
                await client.post(
                    "/api/v1/activity-summaries/generate?kind=monthly", headers=headers
                )
            ).status_code == 422
            assert (
                await client.delete(
                    f"/api/v1/activity-summaries/{response.json()['id']}", headers=headers
                )
            ).status_code == 204
    finally:
        await db.close()
