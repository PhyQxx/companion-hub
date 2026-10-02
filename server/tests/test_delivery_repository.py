from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from test_goal_delivery_runs import database as goal_database
from test_goal_delivery_runs import seed

from app.config.models import RunBudgetConfig
from app.db import CognitiveGoalRecord, Database, TaskRunRecord
from app.harness.budget import BudgetDenied
from app.ids import uuid7
from app.runs.delivery import deliver_once, outcome
from app.runs.delivery_contracts import GoalDeliveryClaim
from app.runs.delivery_sources import (
    SqlDeliverySourceRepository,
    goal_delivery_text,
)

database = goal_database


@pytest.mark.parametrize("mismatch", ["owner", "source"])
async def test_repository_identity_is_fenced_before_run_creation(
    database: Database,
    mismatch: str,
) -> None:
    owner, store, identifier = await seed(database)
    item = (await store.claim_due_goal_reminders())[0]
    assert item.claimed_at
    claim = GoalDeliveryClaim(item.phase, item.claimed_at, "Asia/Shanghai")
    repository = SqlDeliverySourceRepository(CognitiveGoalRecord, identifier, owner, claim)

    async def forbidden() -> list[str]:
        raise AssertionError("a mismatched repository must not dispatch")

    with pytest.raises(BudgetDenied, match="delivery_source_owner_invalid"):
        await deliver_once(
            database,
            source_repository=repository,
            source_id=uuid7() if mismatch == "source" else identifier,
            user_id=uuid7() if mismatch == "owner" else owner,
            text=goal_delivery_text(item.goal.title, item.due_at, claim),
            entry="goal.pre_due",
            config=RunBudgetConfig(),
            dispatch=forbidden,
            run_id=uuid7(),
        )
    async with database.sessions() as session:
        assert not list(await session.scalars(select(TaskRunRecord)))


async def test_repository_revocation_after_channel_return_is_conservative(
    database: Database,
) -> None:
    owner, store, identifier = await seed(database)
    item = (await store.claim_due_goal_reminders(now=datetime.now(UTC)))[0]
    assert item.claimed_at
    claim = GoalDeliveryClaim(item.phase, item.claimed_at, "Asia/Shanghai")
    repository = SqlDeliverySourceRepository(CognitiveGoalRecord, identifier, owner, claim)
    calls = 0

    async def dispatch() -> list[str]:
        nonlocal calls
        calls += 1
        async with database.sessions.begin() as session:
            goal = await session.get(CognitiveGoalRecord, identifier)
            assert goal
            goal.privacy_level = "L2"
        return ["web_chat"]

    with pytest.raises(BudgetDenied, match="delivery_source_private_or_deleted"):
        await deliver_once(
            database,
            source_repository=repository,
            source_id=identifier,
            user_id=owner,
            text=goal_delivery_text(item.goal.title, item.due_at, claim),
            entry="goal.pre_due",
            config=RunBudgetConfig(),
            dispatch=dispatch,
            run_id=uuid7(),
        )
    async with database.sessions() as session:
        rows = list(await session.scalars(select(TaskRunRecord)))
        assert len(rows) == calls == 1
        result = outcome(rows[0])
        assert result and result.status == "unknown"
        assert rows[0].status == "failed"
        assert rows[0].contract["delivery_channels"] == []
        assert rows[0].contract["resource_usage"]["tool_attempts"] == 1
