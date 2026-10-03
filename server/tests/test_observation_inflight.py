"""Revocation during private reads/analysis fences late results and delayed output."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select, update
from test_cognitive_save_guard import database as save_database
from test_observation_owner import accounts
from test_screen_awareness import make_png

from app.browser_awareness import BrowserAnalysis, BrowserAwarenessLoop
from app.config.models import BrowserAwarenessConfig, MailAwarenessConfig, ScreenAwarenessConfig
from app.db import AppUserRecord, Database, TimelineEventRecord
from app.harness.budget import BudgetDenied
from app.ids import uuid7
from app.mail.client import MailSummary
from app.mail_awareness import MailAnalysis, MailAwarenessLoop
from app.screen_awareness import ScreenAwarenessLoop
from app.timeline import TimelineStore

database = save_database


class ObservationPorts:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.device_id = uuid7()
        self.block_stage: str | None = None
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.reads = 0
        self.analyses = 0
        self.memories: list[Any] = []
        self.events: list[tuple[Any, Any, Any]] = []
        self.deliveries: list[Any] = []

    async def hold(self, stage: str, result: Any) -> Any:
        if self.block_stage == stage:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Even a port which returns a late result on cancellation must
                # not be accepted by its caller after revocation.
                self.cancelled.set()
        return result

    async def read(self, *_: Any, **__: Any) -> Any:
        self.reads += 1
        if self.kind == "mail":
            result: Any = [MailSummary(1, "fixture@example.com", "Fixture", None, "Private")]
        elif self.kind == "browser":
            result = SimpleNamespace(origin="https://example.com", title="Fixture", text="Private")
        else:
            result = make_png((200, 200, 200))
        return await self.hold("read", result)

    async def resolve(self, **_: Any) -> Any:
        return SimpleNamespace(id=self.device_id)

    async def analyze(self, **_: Any) -> Any:
        self.analyses += 1
        if self.kind == "mail":
            result: Any = MailAnalysis("Private", True, True, "Fixture")
        elif self.kind == "browser":
            result = BrowserAnalysis("Private", True, True, "Fixture")
        else:
            result = SimpleNamespace(
                text='{"summary":"Private","notable":true,"memory_worthy":true}'
            )
        return await self.hold("analysis", result)

    async def ingest(self, candidate: Any, **_: Any) -> None:
        self.memories.append(candidate)

    def submit(self, event: Any, *, validate: Any, handler: Any, **_: Any) -> None:
        self.events.append((event, validate, handler))

    async def deliver(self, *args: Any, **kwargs: Any) -> None:
        self.deliveries.append((args, kwargs))


async def set_active(database: Database, owner: UUID, active: bool) -> None:
    async with database.sessions.begin() as session:
        await session.execute(
            update(AppUserRecord)
            .where(AppUserRecord.id == owner)
            .values(status="active" if active else "disabled")
        )


async def fixture_loop(database: Database, kind: str) -> tuple[Any, Any, UUID, ObservationPorts]:
    owner = await accounts(database)
    await set_active(database, owner, True)
    ports = ObservationPorts(kind)
    common: dict[str, Any] = {
        "database": database,
        "config_store": SimpleNamespace(),
        "analyzer": ports,
        "timeline": TimelineStore(database),
        "memory_ingester": ports,
        "perception_pipeline": ports,
        "proactive_deliver": ports.deliver,
    }
    loop: Any
    if kind == "mail":
        loop = MailAwarenessLoop(**common, reader=SimpleNamespace(fetch_inbox=ports.read))
        loop.state.baselined = True
        config: Any = MailAwarenessConfig(enabled=True)
    elif kind == "browser":
        loop = BrowserAwarenessLoop(**common, resolver=ports, gateway=cast(Any, ports))
        loop._read_document = ports.read
        config = BrowserAwarenessConfig(enabled=True)
    else:
        loop = ScreenAwarenessLoop(**common, resolver=ports, gateway=cast(Any, ports))
        loop._capture_bytes = ports.read
        config = ScreenAwarenessConfig(enabled=True, displays=[1])
    loop._config_store = SimpleNamespace(
        current=SimpleNamespace(config=SimpleNamespace(**{f"{kind}_awareness": config}))
    )
    return loop, config, owner, ports


@pytest.mark.parametrize("kind", ["mail", "browser", "screen"])
@pytest.mark.parametrize("stage", ["read", "analysis"])
@pytest.mark.parametrize("revocation", ["disable", "replace"])
async def test_inflight_owner_revocation_discards_even_a_late_port_result(
    database: Database, kind: str, stage: str, revocation: str
) -> None:
    loop, config, owner, ports = await fixture_loop(database, kind)
    ports.block_stage = stage
    task = asyncio.create_task(loop._tick(config))
    try:
        await asyncio.wait_for(ports.started.wait(), 2)
        if revocation == "disable":
            await set_active(database, owner, False)
        else:
            async with database.sessions.begin() as session:
                await session.execute(delete(AppUserRecord).where(AppUserRecord.id == owner))
        outcome = (await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2))[0]
        assert outcome is None or (
            isinstance(outcome, BudgetDenied) and outcome.reason_code == "observation_owner_changed"
        )
        assert ports.cancelled.is_set()
        assert ports.analyses == (1 if stage == "analysis" else 0)
        assert not ports.memories and not ports.events and not ports.deliveries
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(TimelineEventRecord)) == 0
        if kind == "screen":
            assert loop.state.displays[1].last_hash is None
            assert loop.state.displays[1].last_summary is None
        else:
            assert loop.state.last_summary is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("kind", ["mail", "browser", "screen"])
async def test_delayed_event_and_delivery_recheck_original_owner(
    database: Database, kind: str
) -> None:
    loop, config, owner, ports = await fixture_loop(database, kind)
    await loop._tick(config)
    assert len(ports.events) == 1
    event, validate, handler = ports.events[0]
    assert await validate()
    await set_active(database, owner, False)
    assert not await validate()
    await handler(event, SimpleNamespace(decision=SimpleNamespace(decision="inform")))
    assert not ports.deliveries


@pytest.mark.parametrize("kind", ["mail", "browser", "screen"])
async def test_owner_revoked_during_indexing_prevents_memory_and_event_derivation(
    database: Database, kind: str
) -> None:
    loop, config, owner, ports = await fixture_loop(database, kind)

    async def index(**_: Any) -> None:
        await set_active(database, owner, False)

    loop._timeline = SimpleNamespace(
        index_custom=index, index_browser_observation=index, index_screen_observation=index
    )
    outcome = (await asyncio.gather(loop._tick(config), return_exceptions=True))[0]
    assert outcome is None or isinstance(outcome, BudgetDenied)
    assert not ports.memories and not ports.events and not ports.deliveries
