"""A completed plan and its report obligation commit together; effects never replay."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_plan_report_runs import database as report_database
from test_plan_report_runs import leaf
from test_propose_action import _registry

from app.cognition import ActionInvocation, ActionPlanService, ActionStepView
from app.cognition.propose import PlanCompletionReporter
from app.db import ActionPlanRecord, AppUserRecord, Database, JobRecord
from app.ids import uuid7
from app.jobs import JobEngine
from app.tools import ToolResult

database = report_database


async def pending(
    database: Database,
    *,
    abort: bool = False,
) -> tuple[ActionPlanService, UUID, UUID]:
    owner = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=owner, display_name="Fixture owner", status="active"))

    async def runner(step: ActionStepView, user_id: UUID) -> ToolResult:
        return ToolResult(ok=True, tool_name=step.tool_name, provider="fixture", latency_ms=0)

    service = ActionPlanService(database, _registry(), runner=runner)
    reporter = PlanCompletionReporter(service)
    service.add_completion_enqueuer(reporter.enqueue_in_session)
    if abort:

        async def fail(session: AsyncSession, plan_id: UUID, user_id: UUID) -> None:
            raise RuntimeError("fixture_transaction_failure")

        service.add_completion_enqueuer(fail)
    plan = await service.create_plan(
        user_id=owner,
        invocations=[ActionInvocation(action_id="test.low_write", arguments={"target": "fixture"})],
        title="Fixture private title",
    )
    if abort:
        with pytest.raises(RuntimeError, match="fixture_transaction_failure"):
            await service.execute_plan(user_id=owner, plan_id=plan.id)
    else:
        await service.execute_plan(user_id=owner, plan_id=plan.id)
    return service, owner, plan.id


async def jobs(database: Database) -> list[JobRecord]:
    async with database.sessions() as session:
        return list(await session.scalars(select(JobRecord)))


async def test_report_obligation_rolls_back_with_completion(database: Database) -> None:
    service, owner, plan_id = await pending(database, abort=True)
    plan = await service.get_plan(user_id=owner, plan_id=plan_id)
    assert plan is not None and plan.status != "completed"
    assert await jobs(database) == []


async def test_restart_reports_committed_plan_without_callback(database: Database) -> None:
    service, owner, plan_id = await pending(database)
    queued = await jobs(database)
    assert len(queued) == 1 and queued[0].status == "queued"
    assert set(queued[0].input) == {"plan_id", "user_id", "source_version", "source_parent_id"}
    assert "Fixture private title" not in str(queued[0].input)
    calls = 0

    async def dispatch(text: str, **kwargs: Any) -> list[str]:
        nonlocal calls
        calls += 1
        assert kwargs["target_user_id"] == owner and "Fixture private title" not in text
        return ["web"]

    restored = PlanCompletionReporter(service, deliver=dispatch)
    await restored.drain()
    assert calls == 1 and (await jobs(database))[0].status == "succeeded"
    assert (await leaf(database, owner)).status == "succeeded"
    async with database.sessions.begin() as session:
        await restored.enqueue_in_session(session, plan_id, owner)
    await PlanCompletionReporter(service, deliver=dispatch).drain()
    assert calls == 1 and len(await jobs(database)) == 1


async def test_no_channel_preserves_queued_obligation(database: Database) -> None:
    service, _, _ = await pending(database)
    reporter = PlanCompletionReporter(service)
    reporter.start()
    await reporter.drain()
    assert (await jobs(database))[0].status == "queued"
    called = asyncio.Event()

    async def dispatch(text: str, **kwargs: Any) -> list[str]:
        called.set()
        return ["web"]

    try:
        reporter.set_deliver(dispatch)
        await asyncio.wait_for(called.wait(), 3)
        await reporter.drain()
        assert (await jobs(database))[0].status == "succeeded"
    finally:
        await reporter.stop()


@pytest.mark.parametrize("change", ["version", "owner", "missing"])
async def test_queued_source_revocation_prevents_dispatch(database: Database, change: str) -> None:
    service, owner, plan_id = await pending(database)
    async with database.sessions.begin() as session:
        if change == "owner":
            await session.execute(
                update(AppUserRecord).where(AppUserRecord.id == owner).values(status="disabled")
            )
        else:
            plan = await session.get(ActionPlanRecord, plan_id)
            assert plan is not None
            if change == "missing":
                await session.delete(plan)
            else:
                plan.title = "Changed"
                plan.updated_at = datetime.now(UTC)

    async def dispatch(text: str, **kwargs: Any) -> list[str]:
        raise AssertionError("revoked source dispatched")

    await PlanCompletionReporter(service, deliver=dispatch).drain()
    assert (await jobs(database))[0].status in {"cancelled", "failed"}


async def test_recovered_job_does_not_repeat_unknown_delivery(database: Database) -> None:
    service, owner, _ = await pending(database)
    calls = 0

    async def dispatch(text: str, **kwargs: Any) -> list[str]:
        nonlocal calls
        calls += 1
        raise RuntimeError("fixture_transport_unknown")

    await PlanCompletionReporter(service, deliver=dispatch).drain()
    assert calls == 1 and (await leaf(database, owner)).status == "failed"
    # Emulate a crash after durable Run settlement, before Job acknowledgement.
    async with database.sessions.begin() as session:
        row = await session.get(JobRecord, (await jobs(database))[0].id)
        assert row is not None
        row.status = "queued"
        row.available_at = datetime.now(UTC) - timedelta(seconds=1)
        row.completed_at = None
    await PlanCompletionReporter(service, deliver=dispatch).drain()
    assert calls == 1 and (await jobs(database))[0].status == "succeeded"
    assert (await leaf(database, owner)).contract["dispatch_state"] == "unknown"


async def test_expired_worker_claim_is_recovered_without_callback(database: Database) -> None:
    service, owner, _ = await pending(database)
    claimed = await JobEngine(database).claim(
        "crashed-fixture", resource_class="plan-completion-report"
    )
    assert claimed is not None
    async with database.sessions.begin() as session:
        await session.execute(
            update(JobRecord)
            .where(JobRecord.id == claimed.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    calls = 0

    async def dispatch(text: str, **kwargs: Any) -> list[str]:
        nonlocal calls
        calls += 1
        return ["web"]

    await PlanCompletionReporter(service, deliver=dispatch).drain()
    assert calls == 1 and (await leaf(database, owner)).status == "succeeded"


async def test_multiple_workers_claim_one_obligation(database: Database) -> None:
    service, _, _ = await pending(database)
    calls = 0

    async def dispatch(text: str, **kwargs: Any) -> list[str]:
        nonlocal calls
        calls += 1
        return ["web"]

    await asyncio.gather(
        *(PlanCompletionReporter(service, deliver=dispatch).drain() for _ in range(8))
    )
    assert calls == 1 and (await jobs(database))[0].status == "succeeded"


@pytest.mark.parametrize("change", ["user_id", "plan_id", "source_version"])
async def test_invalid_obligation_is_cancelled(database: Database, change: str) -> None:
    service, _, _ = await pending(database)
    identifier = (await jobs(database))[0].id
    async with database.sessions.begin() as session:
        row = await session.get(JobRecord, identifier)
        assert row is not None
        row.input = {**row.input, change: "invalid" if change != "source_version" else None}

    async def dispatch(text: str, **kwargs: Any) -> list[str]:
        raise AssertionError("invalid obligation dispatched")

    await PlanCompletionReporter(service, deliver=dispatch).drain()
    assert (await jobs(database))[0].status == "cancelled"


@pytest.mark.parametrize("change", ["stop", "job_cancel"])
async def test_durable_worker_stops_inflight_report(database: Database, change: str) -> None:
    service, owner, _ = await pending(database)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def dispatch(text: str, **kwargs: Any) -> list[str]:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    reporter = PlanCompletionReporter(service, deliver=dispatch)
    reporter.start()
    try:
        await asyncio.wait_for(started.wait(), 3)
        if change == "stop":
            await asyncio.wait_for(reporter.stop(), 3)
        else:
            await JobEngine(database).cancel((await jobs(database))[0].id)
            await asyncio.wait_for(cancelled.wait(), 3)
            await reporter.drain()
        assert cancelled.is_set()
        assert (await jobs(database))[0].status == "cancelled"
        assert (await leaf(database, owner)).contract["dispatch_state"] == "unknown"
    finally:
        await reporter.stop()
