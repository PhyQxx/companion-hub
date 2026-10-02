import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from test_database_config import config_yaml
from test_goal_delivery_runs import database as goal_database
from test_goal_delivery_runs import seed

from app.cognition import (
    AttentionEngine,
    AttentionResult,
    CognitiveCycle,
    CognitiveDecision,
    CognitiveStore,
    RouterDeliberator,
    RuleBasedDeliberator,
    SemanticEvent,
    WorldState,
    WorldStateBuilder,
)
from app.config import ConfigStore
from app.db import CognitiveDecisionRecord, CognitiveGoalRecord, Database, TaskRunRecord
from app.harness.budget import BudgetDenied
from app.ids import uuid7
from app.llm import CompletionRequest, CompletionResult
from app.schemas import PrivacyLevel

database = goal_database


@pytest.mark.parametrize("enabled", [True, False])
async def test_duplicate_model_source_does_not_fall_back_to_another_decision(
    database: Database, tmp_path: Path, enabled: bool
) -> None:
    owner, store, _ = await seed(database)
    event = SemanticEvent(
        event_id=uuid7(),
        user_id=owner,
        kind="user_arrived_home",
        summary="Fixture",
        privacy_level=PrivacyLevel.L1,
        occurred_at=datetime.now(UTC),
        confidence=1,
        evidence_ids=["fixture"],
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    path = tmp_path / "source-claim.yaml"
    path.write_text(config_yaml() + f"\nrun_budget:\n  enabled: {str(enabled).lower()}\n")
    config = ConfigStore(path)
    await config.load()
    started, release = asyncio.Event(), asyncio.Event()
    calls, fallbacks = 0, 0

    class Backend:
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return CompletionResult(
                text='{"decision":"inform","message":"fixture result"}',
                provider="fixture",
                model="fixture",
                endpoint="fixture",
                route=request.route,
                latency_ms=0,
            )

    class Fallback:
        async def deliberate(
            self, actual: SemanticEvent, state: WorldState, attention: AttentionResult
        ) -> CognitiveDecision:
            nonlocal fallbacks
            fallbacks += 1
            return await RuleBasedDeliberator().deliberate(actual, state, attention)

    cycle = CognitiveCycle(
        store,
        WorldStateBuilder(database, store),
        AttentionEngine(threshold=0),
        RouterDeliberator(
            config, database=database, router_builder=lambda _: Backend(), fallback=Fallback()
        ),
    )
    first = asyncio.create_task(cycle.evaluate(event))
    try:
        await asyncio.wait_for(started.wait(), 3)
        competitors = await asyncio.wait_for(
            asyncio.gather(*(cycle.evaluate(event) for _ in range(8)), return_exceptions=True), 5
        )
        assert all(
            isinstance(result, BudgetDenied)
            and result.reason_code == "model_run_source_already_processed"
            for result in competitors
        )
    finally:
        release.set()
        await asyncio.wait_for(first, 3)
    with pytest.raises(BudgetDenied, match="model_run_source_already_processed"):
        await cycle.evaluate(event)
    assert calls == 1 and fallbacks == 0
    async with database.sessions() as session:
        assert len(list(await session.scalars(select(CognitiveDecisionRecord)))) == 1
        roots = list(await session.scalars(select(TaskRunRecord)))
        assert len(roots) == 1 and roots[0].status == "succeeded"


@pytest.mark.parametrize("change", ["privacy", "title", "delete", "completed", "due"])
async def test_changed_goal_blocks_model_before_admission(
    database: Database,
    tmp_path: Path,
    change: str,
) -> None:
    owner, store, identifier = await seed(database)
    event = SemanticEvent(
        event_id=uuid7(),
        occurred_at=datetime.now(UTC),
        user_id=owner,
        kind="user_arrived_home",
        summary="Fixture",
        privacy_level=PrivacyLevel.L1,
        confidence=1,
        evidence_ids=["fixture"],
    )
    state = await WorldStateBuilder(database, store).build(event)
    async with database.sessions.begin() as session:
        goal = await session.get(CognitiveGoalRecord, identifier)
        assert goal
        if change == "privacy":
            goal.privacy_level = "L2"
        elif change == "title":
            goal.title = "Changed"
        elif change == "completed":
            goal.status = "completed"
        elif change == "due":
            goal.due_at = datetime.now(UTC) + timedelta(days=1)
        else:
            await session.delete(goal)
    path = tmp_path / "config.yaml"
    path.write_text(config_yaml())
    config = ConfigStore(path)
    await config.load()

    class Backend:
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            raise AssertionError("revoked source must not reach backend")

    deliberator = RouterDeliberator(config, database=database, router_builder=lambda _: Backend())
    with pytest.raises(BudgetDenied, match="model_source_changed"):
        await deliberator.deliberate(
            event,
            state,
            AttentionResult(
                score=1, threshold=0.5, reason_codes=["fixture"], should_deliberate=True
            ),
        )
    async with database.sessions() as session:
        assert not list(await session.scalars(select(TaskRunRecord)))


@pytest.mark.parametrize("timing", ["inflight", "returned"])
async def test_changed_goal_discards_model_result_and_never_falls_back(
    database: Database,
    tmp_path: Path,
    timing: str,
) -> None:
    owner, store, identifier = await seed(database)
    event = SemanticEvent(
        event_id=uuid7(),
        occurred_at=datetime.now(UTC),
        user_id=owner,
        kind="user_arrived_home",
        summary="Fixture",
        privacy_level=PrivacyLevel.L1,
        confidence=1,
        evidence_ids=["fixture"],
    )
    state = await WorldStateBuilder(database, store).build(event)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def change() -> None:
        async with database.sessions.begin() as session:
            goal = await session.get(CognitiveGoalRecord, identifier)
            assert goal
            goal.privacy_level = "L2"

    class Backend:
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            started.set()
            try:
                if timing == "inflight":
                    await asyncio.sleep(30)
                else:
                    await change()
                return CompletionResult(
                    text='{"decision":"inform","message":"obsolete"}',
                    provider="fixture",
                    model="fixture",
                    endpoint="fixture",
                    route=request.route,
                    latency_ms=0,
                )
            finally:
                stopped.set()

    path = tmp_path / "config.yaml"
    path.write_text(config_yaml())
    config = ConfigStore(path)
    await config.load()
    deliberator = RouterDeliberator(config, database=database, router_builder=lambda _: Backend())
    running = asyncio.create_task(
        deliberator.deliberate(
            event,
            state,
            AttentionResult(
                score=1, threshold=0.5, reason_codes=["fixture"], should_deliberate=True
            ),
        )
    )
    await asyncio.wait_for(started.wait(), 3)
    if timing == "inflight":
        await change()
    with pytest.raises(BudgetDenied, match="model_source_changed"):
        await asyncio.wait_for(running, 3)
    assert stopped.is_set()
    async with database.sessions() as session:
        rows = list(await session.scalars(select(TaskRunRecord)))
        assert len(rows) == 1 and rows[0].status == "failed"
        assert "obsolete" not in str(rows[0].contract)


async def test_owned_private_goal_stays_valid_for_local_scope(database: Database) -> None:
    owner, store, identifier = await seed(database)
    async with database.sessions.begin() as session:
        record = await session.get(CognitiveGoalRecord, identifier)
        assert record
        record.privacy_level = "L2"
    goals = await store.active_goals(
        owner, now=datetime.now(UTC), max_privacy_level=PrivacyLevel.L2
    )
    await store.validate_goal_snapshots(owner, goals, privacy_level=PrivacyLevel.L2)
    with pytest.raises(BudgetDenied, match="model_source_changed"):
        await store.validate_goal_snapshots(owner, goals, privacy_level=PrivacyLevel.L1)


async def test_source_check_failure_does_not_trigger_rule_fallback(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, store, _ = await seed(database)
    event = SemanticEvent(
        event_id=uuid7(),
        occurred_at=datetime.now(UTC),
        user_id=owner,
        kind="user_arrived_home",
        summary="fixture",
        privacy_level=PrivacyLevel.L1,
        evidence_ids=["fixture"],
    )
    state = await WorldStateBuilder(database, store).build(event)

    async def unavailable(*args: object, **kwargs: object) -> None:
        raise RuntimeError("unavailable source repository")

    monkeypatch.setattr(CognitiveStore, "validate_goal_snapshots", unavailable)

    class Backend:
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            raise AssertionError("unverified sources must not reach a provider")

    path = tmp_path / "config.yaml"
    path.write_text(config_yaml())
    config = ConfigStore(path)
    await config.load()
    deliberator = RouterDeliberator(config, database=database, router_builder=lambda _: Backend())
    with pytest.raises(BudgetDenied, match="model_source_check_failed"):
        await deliberator.deliberate(
            event,
            state,
            AttentionResult(
                score=1, threshold=0.5, reason_codes=["fixture"], should_deliberate=True
            ),
        )
