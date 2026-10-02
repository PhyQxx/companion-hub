from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from app.config.models import RunBudgetConfig
from app.db import (
    AppUserRecord,
    Base,
    DailyBriefRecord,
    DailyReviewRecord,
    Database,
    TaskRunEventRecord,
    TaskRunRecord,
    create_database,
)
from app.harness.budget import BudgetDenied, budget_scope
from app.ids import uuid7
from app.runs.budget import RunModelBudget
from app.runs.delivery import SourceTable, deliver_once, outcome, recover_expired_deliveries
from app.runs.store import RunStore


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    value = create_database(f"sqlite+aiosqlite:///{tmp_path / 'delivery.db'}")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield value
    finally:
        await value.close()


async def seed(database: Database, table: SourceTable) -> tuple[UUID, UUID]:
    owner, source = uuid7(), uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=owner, display_name="Fixture", status="active"))
        if table is DailyBriefRecord:
            session.add(
                DailyBriefRecord(
                    id=source,
                    user_id=owner,
                    brief_date=date.today(),
                    facts=[],
                    text="private synthetic body",
                    status="pending",
                )
            )
        else:
            session.add(
                DailyReviewRecord(
                    id=source,
                    user_id=owner,
                    review_date=date.today(),
                    items=[],
                    text="private synthetic body",
                    status="pending",
                )
            )
    return owner, source


@pytest.mark.parametrize("table", [DailyBriefRecord, DailyReviewRecord])
async def test_concurrent_delivery_claims_before_transport(
    database: Database, table: SourceTable
) -> None:
    owner, source = await seed(database, table)
    calls = 0

    async def dispatch() -> list[str]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.1)
        return ["web"]

    async def attempt() -> bool:
        return await deliver_once(
            database,
            table=table,
            source_id=source,
            user_id=owner,
            text="private synthetic body",
            entry="fixture.delivery",
            config=RunBudgetConfig(),
            dispatch=dispatch,
        )

    results = await asyncio.gather(*[attempt() for _ in range(8)])
    assert sum(results) == 1 and calls == 1
    assert not await attempt()
    async with database.sessions() as session:
        run = await session.get(TaskRunRecord, source)
        assert run is not None and run.status == "succeeded"
        view = outcome(run)
        assert view is not None and view.status == "returned" and view.validation_level == "V1"
        events = list(
            await session.scalars(
                select(TaskRunEventRecord).where(TaskRunEventRecord.run_id == source)
            )
        )
        assert "private synthetic body" not in json.dumps(run.contract)
        assert "private synthetic body" not in str([event.payload for event in events])


@pytest.mark.parametrize(
    "mode,expected", [("exception", "unknown"), ("empty", "not_delivered"), ("invalid", "unknown")]
)
async def test_delivery_failure_never_resends(database: Database, mode: str, expected: str) -> None:
    owner, source = await seed(database, DailyBriefRecord)
    calls = 0

    async def dispatch() -> list[str]:
        nonlocal calls
        calls += 1
        if mode == "exception":
            raise RuntimeError("private synthetic error")
        return [] if mode == "empty" else ["private response body with spaces"]

    async def attempt() -> bool:
        return await deliver_once(
            database,
            table=DailyBriefRecord,
            source_id=source,
            user_id=owner,
            text="private synthetic body",
            entry="fixture.delivery",
            config=RunBudgetConfig(),
            dispatch=dispatch,
        )

    if mode == "invalid":
        with pytest.raises(BudgetDenied, match="delivery_channel_receipt_invalid"):
            await attempt()
    else:
        assert await attempt()
    assert not await attempt() and calls == 1
    async with database.sessions() as session:
        run = await session.get(TaskRunRecord, source)
        assert run is not None and run.status == "failed"
        view = outcome(run)
        assert view is not None and view.status == expected and view.validation_level == "V0"
        assert "private" not in str(run.contract.get("delivery_reason"))
        assert run.contract.get("delivery_channels") == []


async def test_stop_cancels_inflight_delivery_and_holds_unknown(database: Database) -> None:
    owner, source = await seed(database, DailyBriefRecord)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def dispatch() -> list[str]:
        started.set()
        try:
            await asyncio.sleep(20)
        finally:
            cancelled.set()
        return ["web"]

    task = asyncio.create_task(
        deliver_once(
            database,
            table=DailyBriefRecord,
            source_id=source,
            user_id=owner,
            text="private synthetic body",
            entry="fixture.delivery",
            config=RunBudgetConfig(),
            dispatch=dispatch,
        )
    )
    await asyncio.wait_for(started.wait(), 3)
    await RunStore(database).cancel_work(source, user_id=owner)
    with pytest.raises(BudgetDenied, match="budget_run_inactive"):
        await asyncio.wait_for(task, 3)
    assert cancelled.is_set()
    async with database.sessions() as session:
        run = await session.get(TaskRunRecord, source)
        assert run is not None and run.status == "cancelled"
        view = outcome(run)
        assert view is not None and view.status == "unknown"
        assert run.contract["resource_usage"]["tool_attempts"] == 1


async def test_parent_budget_reuse_and_recovery_no_replay(database: Database) -> None:
    owner, source = await seed(database, DailyBriefRecord)
    parent = uuid7()
    config = RunBudgetConfig(max_tool_attempts=1)
    async with database.sessions.begin() as session:
        session.add(
            TaskRunRecord(
                id=parent,
                user_id=owner,
                status="running",
                privacy_level="L1",
                contract={"required_work": []},
                budget=config.model_dump(mode="json"),
                deadline=datetime.now(UTC) + timedelta(minutes=5),
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    async def dispatch() -> list[str]:
        return ["web"]

    with budget_scope(RunModelBudget(database, run_id=parent, user_id=owner, config=config)):
        assert await deliver_once(
            database,
            table=DailyBriefRecord,
            source_id=source,
            user_id=owner,
            text="private synthetic body",
            entry="fixture.delivery",
            config=config,
            dispatch=dispatch,
        )
    async with database.sessions.begin() as session:
        run = await session.get(TaskRunRecord, source)
        parent_run = await session.get(TaskRunRecord, parent)
        assert run is not None and run.parent_run_id == parent and run.budget is None
        assert (
            parent_run is not None and parent_run.contract["resource_usage"]["tool_attempts"] == 1
        )
        # Simulate persisted crash state without invoking an external transport.
        run.status = "running"
        run.contract = {**run.contract, "dispatch_state": "started"}
        run.deadline = datetime.now(UTC) - timedelta(seconds=1)
    assert await recover_expired_deliveries(database) == 1
    assert await recover_expired_deliveries(database) == 0
    async with database.sessions() as session:
        run = await session.get(TaskRunRecord, source)
        assert run is not None
        view = outcome(run)
        assert view is not None and view.status == "unknown"


async def test_changed_source_blocks_transport_even_with_budget_disabled(
    database: Database,
) -> None:
    owner, source = await seed(database, DailyBriefRecord)

    async def dispatch() -> list[str]:
        pytest.fail("must not dispatch")

    with pytest.raises(BudgetDenied, match="delivery_source_changed"):
        await deliver_once(
            database,
            table=DailyBriefRecord,
            source_id=source,
            user_id=owner,
            text="stale body",
            entry="fixture.delivery",
            config=RunBudgetConfig(enabled=False),
            dispatch=dispatch,
        )
    async with database.sessions() as session:
        assert await session.get(TaskRunRecord, source) is None
