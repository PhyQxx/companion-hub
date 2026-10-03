"""Different event keys share one durable proactive quota at decision acceptance."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from test_cognitive_save_guard import database as save_database
from test_delivery_sqlite_transactions import explicit_transactions
from test_observation_owner_binding import accounts
from test_perception import create_pipeline, event

from app.cognition import CognitiveCycle, CognitiveStore, RuleBasedDeliberator
from app.cognition.models import AttentionResult, CognitiveDecision, SemanticEvent, WorldState
from app.cognition.proactive import daily_window
from app.db import CognitiveDecisionRecord, Database, ProactiveQuotaEntryRecord
from app.perception import PerceptionDisposition

database = save_database


async def retained_count(database: Database) -> int:
    async with database.sessions.begin() as session:
        return int(
            await session.scalar(select(func.count()).select_from(ProactiveQuotaEntryRecord)) or 0
        )


async def test_competing_event_keys_cannot_exceed_one_daily_slot(database: Database) -> None:
    owner, _ = await accounts(database)
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0
    rule = RuleBasedDeliberator()

    class Barrier:
        async def deliberate(
            self, value: SemanticEvent, state: WorldState, attention: AttentionResult
        ) -> CognitiveDecision:
            nonlocal count
            count += 1
            if count == 12:
                entered.set()
            await release.wait()
            return await rule.deliberate(value, state, attention)

    pipelines = [create_pipeline(database, daily_limit=1) for _ in range(12)]
    for pipeline in pipelines:
        assert isinstance(pipeline._cycle, CognitiveCycle)
        pipeline._cycle.deliberator = Barrier()
    tasks = [
        asyncio.create_task(
            pipeline.process(event(owner, "user_arrived_home", dedupe_key=f"different-key:{index}"))
        )
        for index, pipeline in enumerate(pipelines)
    ]
    try:
        await asyncio.wait_for(entered.wait(), 10)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 15)
        visible = [
            result for result in results if result.decision and result.decision.decision != "ignore"
        ]
        assert len(visible) == 1
        assert sum(result.reason_code == "daily_limit" for result in results) == 11
        assert all(
            result.disposition == PerceptionDisposition.SUPPRESSED
            for result in results
            if result.reason_code == "daily_limit"
        )
        assert await retained_count(database) == 1
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_deleting_decision_cannot_reset_spent_daily_quota(database: Database) -> None:
    owner, _ = await accounts(database)
    pipeline = create_pipeline(database, daily_limit=1)
    first = await pipeline.process(event(owner, "user_arrived_home", dedupe_key="first"))
    assert first.decision is not None and first.decision.decision == "suggest"
    async with database.sessions.begin() as session:
        await session.execute(
            delete(CognitiveDecisionRecord).where(CognitiveDecisionRecord.user_id == owner)
        )
    second = await pipeline.process(event(owner, "user_arrived_home", dedupe_key="second"))
    assert second.reason_code == "daily_limit"
    assert second.disposition == PerceptionDisposition.SUPPRESSED
    assert await retained_count(database) == 1


async def test_provider_timestamp_cannot_backdate_quota_consumption(database: Database) -> None:
    owner, _ = await accounts(database)
    pipeline = create_pipeline(database, daily_limit=1)
    rule = RuleBasedDeliberator()

    class Backdated:
        async def deliberate(
            self, value: SemanticEvent, state: WorldState, attention: AttentionResult
        ) -> CognitiveDecision:
            decision = await rule.deliberate(value, state, attention)
            return decision.model_copy(
                update={"created_at": decision.created_at - timedelta(days=2)}
            )

    assert isinstance(pipeline._cycle, CognitiveCycle)
    pipeline._cycle.deliberator = Backdated()
    first = await pipeline.process(event(owner, "user_arrived_home", dedupe_key="first"))
    assert first.decision is not None and first.decision.decision == "suggest"
    second = await pipeline.process(event(owner, "user_arrived_home", dedupe_key="second"))
    assert second.reason_code == "daily_limit"


async def test_critical_event_bypasses_cap_and_consumes_a_count(database: Database) -> None:
    owner, _ = await accounts(database)
    pipeline = create_pipeline(database, daily_limit=1)
    first = await pipeline.process(event(owner, "user_arrived_home", dedupe_key="first"))
    assert first.decision is not None and first.decision.decision == "suggest"
    critical = await pipeline.process(event(owner, "water_leak", dedupe_key="critical"))
    assert critical.decision is not None and critical.decision.decision == "escalate"
    normal = await pipeline.process(event(owner, "user_arrived_home", dedupe_key="normal"))
    assert normal.reason_code == "daily_limit"
    assert await retained_count(database) == 2


async def test_ignored_and_recorded_events_do_not_spend_a_slot(database: Database) -> None:
    owner, _ = await accounts(database)
    pipeline = create_pipeline(database, daily_limit=1)
    ignored = event(owner, "user_arrived_home", dedupe_key="ignored").model_copy(
        update={"confidence": 0.01}
    )
    recorded = event(owner, "user_arrived_home", dedupe_key="recorded").model_copy(
        update={"passive": True}
    )
    assert (await pipeline.process(ignored)).decision is not None
    assert (await pipeline.process(recorded)).decision is not None
    assert await retained_count(database) == 0
    result = await pipeline.process(event(owner, "user_arrived_home", dedupe_key="visible"))
    assert result.decision is not None and result.decision.decision == "suggest"
    assert await retained_count(database) == 1


async def test_failed_acceptance_rolls_back_quota_entry(database: Database) -> None:
    owner, _ = await accounts(database)
    source = event(owner, "user_arrived_home", dedupe_key="rolled-back")
    decision = await RuleBasedDeliberator().deliberate(
        source,
        WorldState(built_at=datetime.now(UTC), timezone="UTC"),
        AttentionResult(score=1, threshold=0.5, reason_codes=[], should_deliberate=True),
    )
    store = CognitiveStore(database)
    await store.save_decision(
        decision.model_copy(update={"decision": "ignore", "message": None}), event=source
    )
    with pytest.raises(IntegrityError):
        await store.save_decision(decision, event=source, proactive_limit=1)
    assert await retained_count(database) == 0
    result = await create_pipeline(database, daily_limit=1).process(
        event(owner, "user_arrived_home", dedupe_key="next")
    )
    assert result.decision is not None and result.decision.decision == "suggest"
    assert await retained_count(database) == 1


async def test_legacy_decisions_count_once_without_backfilling_body(database: Database) -> None:
    owner, _ = await accounts(database)
    source = event(owner, "user_arrived_home", dedupe_key="legacy")
    decision = await RuleBasedDeliberator().deliberate(
        source,
        WorldState(built_at=datetime.now(UTC), timezone="UTC"),
        AttentionResult(score=1, threshold=0.5, reason_codes=[], should_deliberate=True),
    )
    await CognitiveStore(database).save_decision(decision, event=source)
    assert await retained_count(database) == 0
    result = await create_pipeline(database, daily_limit=1).process(
        event(owner, "user_arrived_home", dedupe_key="current")
    )
    assert result.reason_code == "daily_limit"


async def test_separate_owners_have_independent_daily_quota(database: Database) -> None:
    first, second = await accounts(database)
    pipeline = create_pipeline(database, daily_limit=1)
    for owner in (first, second):
        result = await pipeline.process(event(owner, "user_arrived_home", dedupe_key="same-kind"))
        assert result.decision is not None and result.decision.decision == "suggest"
    assert await retained_count(database) == 2


async def test_competing_quota_with_explicit_sqlite_transactions(database: Database) -> None:
    async with explicit_transactions(database):
        await test_competing_event_keys_cannot_exceed_one_daily_slot(database)


@pytest.mark.parametrize(
    "moment, hours",
    [
        (datetime(2026, 3, 8, 12, tzinfo=UTC), 23),
        (datetime(2026, 11, 1, 12, tzinfo=UTC), 25),
    ],
)
def test_daily_window_uses_next_local_midnight_across_dst(moment: datetime, hours: int) -> None:
    start, end = daily_window(moment, "America/New_York")
    assert end - start == timedelta(hours=hours)
    assert start <= moment < end


def test_unknown_timezone_uses_shanghai_calendar() -> None:
    moment = datetime(2026, 10, 3, 3, tzinfo=UTC)
    assert daily_window(moment, "fixture/missing") == daily_window(moment, "Asia/Shanghai")
