"""Completion reports are bounded delivery attempts, independent of action execution."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select, update
from test_propose_action import _registry

from app.cognition import ActionInvocation, ActionPlanService, ActionStepView
from app.cognition.propose import PlanCompletionReporter
from app.config.models import RunBudgetConfig
from app.db import (
    ActionPlanRecord,
    AppUserRecord,
    Base,
    Database,
    TaskRunEventRecord,
    TaskRunRecord,
    create_database,
)
from app.ids import uuid7
from app.tools import ToolResult


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    value = create_database(f"sqlite+aiosqlite:///{tmp_path / 'reports.db'}")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield value
    finally:
        await value.close()


async def completed(
    database: Database,
    *,
    parent: bool = False,
) -> tuple[ActionPlanService, UUID, UUID, UUID | None]:
    owner, parent_id = uuid7(), uuid7() if parent else None
    now = datetime.now(UTC)
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=owner, display_name="Synthetic owner", status="active"))
        await session.flush()
        if parent_id:
            session.add(
                TaskRunRecord(
                    id=parent_id,
                    user_id=owner,
                    status="succeeded",
                    privacy_level="L1",
                    contract={"criterion": "reply_committed", "required_work": []},
                    budget=RunBudgetConfig(max_tool_attempts=1).model_dump(mode="json"),
                    created_at=now,
                    updated_at=now,
                )
            )

    async def runner(step: ActionStepView, user_id: UUID) -> ToolResult:
        return ToolResult(ok=True, tool_name=step.tool_name, provider="fixture", latency_ms=0)

    service = ActionPlanService(database, _registry(), runner=runner)
    plan = await service.create_plan(
        user_id=owner,
        invocations=[ActionInvocation(action_id="test.low_write", arguments={"target": "fixture"})],
        title="Synthetic report",
        source_turn_id=parent_id,
    )
    await service.execute_plan(user_id=owner, plan_id=plan.id)
    return service, owner, plan.id, parent_id


async def leaf(database: Database, owner: UUID) -> TaskRunRecord:
    async with database.sessions() as session:
        result = await session.scalar(
            select(TaskRunRecord).where(
                TaskRunRecord.user_id == owner,
                TaskRunRecord.contract["entry"].as_string() == "plan.completed.delivery",
            )
        )
        assert result is not None
        return result


async def tool_attempts(database: Database, run_id: UUID) -> int:
    async with database.sessions() as session:
        return int(
            await session.scalar(
                select(func.count(TaskRunEventRecord.event_id)).where(
                    TaskRunEventRecord.run_id == run_id,
                    TaskRunEventRecord.kind == "run.tool.reserved",
                )
            )
            or 0
        )


async def test_same_completed_plan_has_one_report_across_reporters(database: Database) -> None:
    service, owner, plan_id, _ = await completed(database)
    calls: list[str] = []

    async def dispatch(text: str, **kwargs: Any) -> list[str]:
        calls.append(text)
        assert kwargs["target_user_id"] == owner
        assert "计划详情" in text
        assert "Synthetic report" not in text and "test.low_write" not in text
        return ["web"]

    reporters = [PlanCompletionReporter(service, deliver=dispatch) for _ in range(8)]
    for reporter in reporters:
        reporter.on_plan_completed(plan_id, owner)
    await asyncio.gather(*(reporter.drain() for reporter in reporters))
    assert len(calls) == 1
    result = await leaf(database, owner)
    assert result.status == "succeeded" and await tool_attempts(database, result.id) == 1
    assert result.contract["delivery_channels"] == ["web"]
    for reporter in reporters:
        reporter.on_plan_completed(plan_id, owner)
    await asyncio.gather(*(reporter.drain() for reporter in reporters))
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["plan", "owner", "stop", "parent", "privacy", "deadline"])
async def test_inflight_report_stops_without_repeating_executed_actions(
    database: Database,
    change: str,
) -> None:
    service, owner, plan_id, parent_id = await completed(database, parent=True)
    started, cancelled = asyncio.Event(), asyncio.Event()
    calls = 0

    async def dispatch(text: str, **kwargs: Any) -> list[str]:
        nonlocal calls
        calls += 1
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    reporter = PlanCompletionReporter(service, deliver=dispatch)
    try:
        reporter.on_plan_completed(plan_id, owner)
        await asyncio.wait_for(started.wait(), timeout=3)
        if change == "stop":
            await asyncio.wait_for(reporter.stop(), timeout=2)
        else:
            async with database.sessions.begin() as session:
                if change == "plan":
                    await session.execute(
                        update(ActionPlanRecord)
                        .where(ActionPlanRecord.id == plan_id)
                        .values(title="Changed", updated_at=datetime.now(UTC))
                    )
                elif change == "owner":
                    await session.execute(
                        update(AppUserRecord)
                        .where(AppUserRecord.id == owner)
                        .values(status="disabled")
                    )
                elif change == "deadline":
                    current = await leaf(database, owner)
                    await session.execute(
                        update(TaskRunRecord)
                        .where(TaskRunRecord.id == current.id)
                        .values(deadline=datetime.now(UTC) - timedelta(seconds=1))
                    )
                else:
                    await session.execute(
                        update(TaskRunRecord)
                        .where(TaskRunRecord.id == parent_id)
                        .values(contract={"work_cancel_requested": True})
                        if change == "parent"
                        else update(TaskRunRecord)
                        .where(TaskRunRecord.id == parent_id)
                        .values(privacy_level="L2")
                    )
            await asyncio.wait_for(reporter.drain(), timeout=2)
        assert cancelled.is_set()
        result = await leaf(database, owner)
        assert result.status in {"failed", "cancelled"}
        assert result.contract["dispatch_state"] == "unknown"
        assert (await service.get_plan(user_id=owner, plan_id=plan_id)).status == "completed"
        reporter.on_plan_completed(plan_id, owner)
        await reporter.drain()
        assert calls == 1
    finally:
        await reporter.stop()


@pytest.mark.parametrize("enabled", [False, True])
async def test_failed_report_preserves_unknown_without_resending(
    database: Database, enabled: bool
) -> None:
    service, owner, plan_id, _ = await completed(database)
    calls = 0

    async def dispatch(text: str, **kwargs: Any) -> list[str]:
        nonlocal calls
        calls += 1
        raise RuntimeError("synthetic failure")

    reporter = PlanCompletionReporter(
        service, deliver=dispatch, budget_loader=lambda: RunBudgetConfig(enabled=enabled)
    )
    reporter.on_plan_completed(plan_id, owner)
    await reporter.drain()
    reporter.on_plan_completed(plan_id, owner)
    await reporter.drain()
    result = await leaf(database, owner)
    assert result.status == "failed" and result.contract["dispatch_state"] == "unknown"
    assert await tool_attempts(database, result.id) == (1 if enabled else 0) and calls == 1


async def test_report_reuses_parent_tool_budget(database: Database) -> None:
    service, owner, plan_id, parent_id = await completed(database, parent=True)

    async def dispatch(text: str, **kwargs: Any) -> list[str]:
        return ["web"]

    reporter = PlanCompletionReporter(service, deliver=dispatch)
    reporter.on_plan_completed(plan_id, owner)
    await reporter.drain()
    result = await leaf(database, owner)
    assert result.parent_run_id == parent_id and await tool_attempts(database, result.id) == 0
    assert result.contract["source_parent_run_id"] == str(parent_id)
    async with database.sessions() as session:
        parent = await session.get(TaskRunRecord, parent_id)
        assert parent is not None
        events = list(
            await session.scalars(
                select(TaskRunEventRecord).where(TaskRunEventRecord.run_id == parent_id)
            )
        )
        assert sum(event.kind == "run.tool.reserved" for event in events) == 1


async def test_report_cannot_obtain_new_quota_after_parent_budget_exhaustion(
    database: Database,
) -> None:
    from app.runs.resources import RunToolBudget

    service, owner, plan_id, parent_id = await completed(database, parent=True)
    assert parent_id is not None
    budget = RunToolBudget(
        database, run_id=parent_id, user_id=owner, config=RunBudgetConfig(), maintenance=True
    )
    permit = await budget.reserve_tool(tool_name="fixture.previous", user_id=owner)
    await budget.settle_tool(permit.call_id, reported_ok=True)

    async def dispatch(text: str, **kwargs: Any) -> list[str]:
        raise AssertionError("exhausted source must not get an independent quota")

    reporter = PlanCompletionReporter(service, deliver=dispatch)
    reporter.on_plan_completed(plan_id, owner)
    await reporter.drain()
    result = await leaf(database, owner)
    assert result.status == "failed" and result.contract["dispatch_state"] == "not_started"
    assert result.contract["delivery_reason"] == "tool_budget_exhausted"
    assert await tool_attempts(database, parent_id) == 1
    reporter.on_plan_completed(plan_id, owner)
    await reporter.drain()
    assert await tool_attempts(database, parent_id) == 1


async def test_private_parent_is_rejected_before_new_report_run(database: Database) -> None:
    service, owner, plan_id, parent_id = await completed(database, parent=True)
    async with database.sessions.begin() as session:
        await session.execute(
            update(TaskRunRecord).where(TaskRunRecord.id == parent_id).values(privacy_level="L2")
        )

    async def dispatch(text: str, **kwargs: Any) -> list[str]:
        raise AssertionError("private source cannot use L1 report")

    reporter = PlanCompletionReporter(service, deliver=dispatch)
    reporter.on_plan_completed(plan_id, owner)
    await reporter.drain()
    async with database.sessions() as session:
        ids = list(await session.scalars(select(TaskRunRecord.id)))
    assert ids == [parent_id]


async def test_absent_channel_does_not_claim_and_shutdown_prevents_new_reports(
    database: Database,
) -> None:
    service, owner, plan_id, _ = await completed(database)
    reporter = PlanCompletionReporter(service)
    reporter.on_plan_completed(plan_id, owner)
    await reporter.drain()
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(TaskRunRecord.id))) == 0

    async def dispatch(text: str, **kwargs: Any) -> list[str]:
        raise AssertionError("stopped reporter must not dispatch")

    reporter.set_deliver(dispatch)
    await reporter.stop()
    reporter.on_plan_completed(plan_id, owner)
    assert not reporter._background


@pytest.mark.parametrize("foreign", [False, True])
async def test_typed_channel_receipt_binds_owner(database: Database, foreign: bool) -> None:
    from app.output import ProactiveChannelAttempt, ProactiveDeliveryResult

    service, owner, plan_id, _ = await completed(database)

    async def dispatch(text: str, **kwargs: Any) -> ProactiveDeliveryResult:
        return ProactiveDeliveryResult(
            user_id=uuid7() if foreign else owner,
            conversation_id=None,
            attempts=(ProactiveChannelAttempt("web", True),),
        )

    reporter = PlanCompletionReporter(service, deliver=dispatch)
    reporter.on_plan_completed(plan_id, owner)
    await reporter.drain()
    result = await leaf(database, owner)
    assert result.status == ("failed" if foreign else "succeeded")
    assert result.contract["dispatch_state"] == ("unknown" if foreign else "returned")
    assert result.contract["delivery_channels"] == ([] if foreign else ["web"])


@pytest.mark.parametrize("change", ["privacy", "message_intent", "conversation_intent"])
async def test_known_message_lineage_is_revocable_before_deletion_replay(
    database: Database, change: str
) -> None:
    from app.db import ConversationRecord, DeletionLedgerRecord, MessageRecord

    service, owner, plan_id, parent_id = await completed(database, parent=True)
    conversation_id, message_id = uuid7(), uuid7()
    now = datetime.now(UTC)
    async with database.sessions.begin() as session:
        session.add(
            ConversationRecord(id=conversation_id, user_id=owner, status="active", created_at=now)
        )
        await session.flush()
        session.add(
            MessageRecord(
                id=message_id,
                conversation_id=conversation_id,
                turn_id=uuid7(),
                seq=1,
                role="user",
                content="synthetic input",
                privacy_level="L1",
                created_at=now,
            )
        )
        parent = await session.get(TaskRunRecord, parent_id)
        assert parent is not None
        parent.conversation_id = conversation_id
        parent.contract = {**parent.contract, "input_message_id": str(message_id)}
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def dispatch(text: str, **kwargs: Any) -> list[str]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    reporter = PlanCompletionReporter(service, deliver=dispatch)
    try:
        reporter.on_plan_completed(plan_id, owner)
        await asyncio.wait_for(started.wait(), timeout=3)
        async with database.sessions.begin() as session:
            if change == "privacy":
                await session.execute(
                    update(MessageRecord)
                    .where(MessageRecord.id == message_id)
                    .values(privacy_level="L2")
                )
            else:
                target = message_id if change == "message_intent" else conversation_id
                session.add(
                    DeletionLedgerRecord(
                        entity_kind="message",
                        entity_id=target.hex.upper(),
                        deleted_ids=[],
                        requested_by=str(owner),
                    )
                )
        await asyncio.wait_for(reporter.drain(), timeout=2)
        assert cancelled.is_set()
        result = await leaf(database, owner)
        assert result.status == "cancelled" and result.contract["dispatch_state"] == "unknown"
        assert (await service.get_plan(user_id=owner, plan_id=plan_id)).status == "completed"
    finally:
        await reporter.stop()
