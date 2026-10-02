"""Disabling the implicit source owner must not assign private observations elsewhere."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import update
from test_cognitive_save_guard import database as save_database

from app.browser_awareness import BrowserAwarenessLoop
from app.context.owners import observation_owner
from app.db import AppUserRecord, Database
from app.ids import uuid7
from app.mail_awareness import MailAwarenessLoop
from app.screen_awareness import ScreenAwarenessLoop
from app.timeline import TimelineStore

database = save_database


async def accounts(database: Database) -> UUID:
    primary, secondary = uuid7(), uuid7()
    now = datetime.now(UTC)
    async with database.sessions.begin() as session:
        session.add_all(
            [
                AppUserRecord(
                    id=primary,
                    display_name="Primary fixture",
                    status="disabled",
                    created_at=now - timedelta(days=1),
                ),
                AppUserRecord(
                    id=secondary, display_name="Other fixture", status="active", created_at=now
                ),
            ]
        )
    return primary


async def test_primary_source_owner_pauses_and_resumes_without_reassignment(
    database: Database,
) -> None:
    assert await observation_owner(database) is None
    primary = await accounts(database)
    assert await observation_owner(database) is None
    async with database.sessions.begin() as session:
        await session.execute(
            update(AppUserRecord).where(AppUserRecord.id == primary).values(status="active")
        )
    assert await observation_owner(database) == primary
    async with database.sessions.begin() as session:
        await session.execute(
            update(AppUserRecord).where(AppUserRecord.id == primary).values(status="disabled")
        )
    assert await observation_owner(database) is None


@pytest.mark.parametrize("kind", ["mail", "browser", "screen"])
async def test_inactive_owner_tick_never_reads_private_source(
    database: Database, kind: str
) -> None:
    await accounts(database)
    calls: list[str] = []

    class ForbiddenPorts:
        def __getattr__(self, name: str) -> Any:
            calls.append(name)
            raise AssertionError("inactive owner's private source was accessed")

    ports: Any = ForbiddenPorts()
    common: dict[str, Any] = {
        "database": database,
        "config_store": ports,
        "analyzer": ports,
        "timeline": TimelineStore(database),
    }
    loop: Any
    if kind == "mail":
        loop = MailAwarenessLoop(**common, reader=ports)
    elif kind == "browser":
        loop = BrowserAwarenessLoop(**common, resolver=ports, gateway=ports)
    else:
        loop = ScreenAwarenessLoop(**common, resolver=ports, gateway=ports)
    await loop._tick(SimpleNamespace(enabled=True))
    assert calls == []
