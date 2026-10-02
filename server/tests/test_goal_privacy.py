from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from app.cognition import CognitiveStore, GoalKind, GoalStatus
from app.cognition.models import SemanticEvent
from app.cognition.world import WorldStateBuilder
from app.config.models import RunBudgetConfig
from app.db import (
    AppUserRecord,
    Base,
    CognitiveGoalRecord,
    ConversationRecord,
    DailyBriefRecord,
    Database,
    DeletionLedgerRecord,
    MessageRecord,
    TaskRunRecord,
    create_database,
)
from app.harness.budget import BudgetDenied
from app.ids import uuid7
from app.runs.delivery import deliver_once
from app.schemas.common import PrivacyLevel
from app.tasks.brief import DailyBriefService
from app.tasks.goal_scheduler import GoalReminderScheduler
from app.tasks.review import DailyReviewService
from app.tasks.store import TaskStore

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    value = create_database(f"sqlite+aiosqlite:///{tmp_path / 'privacy.db'}")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield value
    finally:
        await value.close()


async def seed(database: Database, privacy: str = "L2") -> tuple[UUID, UUID, UUID]:
    owner, conversation, message = uuid7(), uuid7(), uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=owner, display_name="Fixture", status="active"))
        session.add(
            ConversationRecord(
                id=conversation,
                user_id=owner,
                title="Fixture",
                status="active",
                created_at=NOW,
                last_active_at=NOW,
            )
        )
        session.add(
            MessageRecord(
                id=message,
                conversation_id=conversation,
                turn_id=uuid7(),
                seq=1,
                role="user",
                content="private synthetic source",
                privacy_level=privacy,
                created_at=NOW,
            )
        )
    return owner, conversation, message


async def private_goal(database: Database, owner: UUID, message: UUID) -> CognitiveGoalRecord:
    goal = await CognitiveStore(database).create_goal(
        user_id=owner,
        kind=GoalKind.USER,
        title="private synthetic goal",
        source_kind="message",
        source_id=str(message),
        due_at=NOW + timedelta(hours=1),
        now=NOW,
    )
    async with database.sessions() as session:
        row = await session.get(CognitiveGoalRecord, goal.id)
        assert row is not None
        return row


@pytest.mark.parametrize("declared,source", [("L2", "L1"), ("L1", "L2"), ("L0", "L2")])
async def test_goal_label_never_lowers_requested_or_inherited_privacy(
    database: Database, declared: str, source: str
) -> None:
    owner, _, message = await seed(database, source)
    store = CognitiveStore(database)
    goal = await store.create_goal(
        user_id=owner,
        kind=GoalKind.USER,
        title="private synthetic goal",
        source_kind="message",
        source_id=str(message),
        privacy_level=PrivacyLevel(declared),
        due_at=NOW + timedelta(hours=1),
        now=NOW,
    )
    assert goal.privacy_level == "L2"
    assert await store.active_goals(owner, now=NOW, max_privacy_level=PrivacyLevel.L1) == []
    assert [
        row.id
        for row in await store.active_goals(owner, now=NOW, max_privacy_level=PrivacyLevel.L2)
    ] == [goal.id]
    assert [row.id for row in await store.active_goals(owner, now=NOW)] == [goal.id]


async def test_private_goals_do_not_enter_brief_review_or_public_world(database: Database) -> None:
    owner, _, message = await seed(database)
    await private_goal(database, owner, message)
    store = CognitiveStore(database)
    await store.create_goal(
        user_id=owner,
        kind=GoalKind.USER,
        title="public fixture",
        source_kind="manual",
        source_id="fixture",
        due_at=NOW + timedelta(hours=1),
        now=NOW,
    )
    brief = await DailyBriefService(database, TaskStore(database), store, clock=lambda: NOW).build(
        owner
    )
    review = await DailyReviewService(
        database, TaskStore(database), store, clock=lambda: NOW
    ).build(owner)
    assert "private synthetic goal" not in brief.text + review.text
    assert "public fixture" in brief.text + review.text
    builder = WorldStateBuilder(database, store)
    event = SemanticEvent(
        event_id=uuid7(),
        user_id=owner,
        kind="fixture",
        summary="fixture",
        occurred_at=NOW,
        privacy_level=PrivacyLevel.L1,
        evidence_ids=["synthetic:fixture"],
    )
    public = await builder.build(event, now=NOW)
    assert [goal.title for goal in public.active_goals] == ["public fixture"]
    assert public.last_interaction_at is None
    private = await builder.build(
        event.model_copy(update={"privacy_level": PrivacyLevel.L2}), now=NOW
    )
    assert private.last_interaction_at is not None
    assert private.last_interaction_at.replace(tzinfo=UTC) == NOW
    assert {goal.title for goal in private.active_goals} == {
        "public fixture",
        "private synthetic goal",
    }
    ephemeral = await builder.build(
        event.model_copy(update={"privacy_level": PrivacyLevel.L3}), now=NOW
    )
    assert ephemeral.active_goals == []


async def test_legacy_goals_use_origin_and_completed_private_goals_stay_out_of_review(
    database: Database,
) -> None:
    owner, _, message = await seed(database)
    goal = await private_goal(database, owner, message)
    async with database.sessions.begin() as session:
        row = await session.get(CognitiveGoalRecord, goal.id)
        assert row is not None
        row.privacy_level = None  # An actual pre-migration row.
    store = CognitiveStore(database)
    assert await store.active_goals(owner, now=NOW, max_privacy_level=PrivacyLevel.L1) == []
    assert len(await store.active_goals(owner, now=NOW, max_privacy_level=PrivacyLevel.L2)) == 1
    await store.set_goal_status(
        user_id=owner, goal_id=goal.id, status=GoalStatus.COMPLETED, now=NOW
    )
    review = await DailyReviewService(
        database, TaskStore(database), store, clock=lambda: NOW
    ).build(owner)
    assert "private synthetic goal" not in review.text


async def test_private_manual_goal_and_l3_rejection(database: Database) -> None:
    owner, _, _ = await seed(database, "L1")
    store = CognitiveStore(database)
    await store.create_goal(
        user_id=owner,
        kind=GoalKind.USER,
        title="private manual",
        source_kind="manual",
        source_id="fixture",
        privacy_level=PrivacyLevel.L2,
        due_at=NOW + timedelta(hours=1),
        now=NOW,
    )
    assert await store.active_goals(owner, now=NOW, max_privacy_level=PrivacyLevel.L1) == []
    assert len(await store.active_goals(owner, now=NOW, max_privacy_level=PrivacyLevel.L2)) == 1
    with pytest.raises(ValueError, match="L3"):
        await store.create_goal(
            user_id=owner,
            kind=GoalKind.USER,
            title="ephemeral",
            source_kind="manual",
            source_id="fixture",
            privacy_level=PrivacyLevel.L3,
            now=NOW,
        )


async def test_message_tombstone_blocks_legacy_goal_before_restore_replay(
    database: Database,
) -> None:
    owner, conversation, message = await seed(database, "L1")
    await private_goal(database, owner, message)
    async with database.sessions.begin() as session:
        session.add(
            DeletionLedgerRecord(
                entity_kind="message",
                entity_id=str(conversation),
                deleted_ids=[],
                requested_by=str(owner),
                created_at=NOW,
            )
        )
    store = CognitiveStore(database)
    assert await store.active_goals(owner, now=NOW, max_privacy_level=PrivacyLevel.L1) == []
    assert all(item.user_id != owner for item in await store.claim_due_goal_reminders(now=NOW))


async def test_goal_reminder_revalidates_claim_after_privacy_change(database: Database) -> None:
    owner, _, message = await seed(database, "L1")
    await private_goal(database, owner, message)
    store = CognitiveStore(database)
    claimed = await store.claim_due_goal_reminders(now=NOW)
    assert len(claimed) == 1
    async with database.sessions.begin() as session:
        row = await session.get(MessageRecord, message)
        assert row is not None
        row.privacy_level = "L2"
    calls = 0

    async def dispatch(text: str, **kwargs: object) -> list[str]:
        nonlocal calls
        calls += 1
        return ["web_chat"]

    scheduler = GoalReminderScheduler(store, deliverer=dispatch)
    await scheduler._remind(claimed[0])
    assert calls == 0


async def test_old_pending_brief_with_private_goal_is_not_dispatched(database: Database) -> None:
    owner, _, message = await seed(database)
    goal = await private_goal(database, owner, message)
    source = uuid7()
    async with database.sessions.begin() as session:
        session.add(
            DailyBriefRecord(
                id=source,
                user_id=owner,
                brief_date=NOW.date(),
                status="pending",
                text="private synthetic goal",
                facts=[
                    {"kind": "goal", "text": "private synthetic goal", "source": f"goal:{goal.id}"}
                ],
            )
        )

    async def dispatch() -> list[str]:
        pytest.fail("must not dispatch private source")

    with pytest.raises(BudgetDenied, match="delivery_source_private_or_deleted"):
        await deliver_once(
            database,
            table=DailyBriefRecord,
            source_id=source,
            user_id=owner,
            text="private synthetic goal",
            entry="brief.delivery",
            config=RunBudgetConfig(),
            dispatch=dispatch,
        )
    async with database.sessions() as session:
        assert await session.get(TaskRunRecord, source) is None


async def test_public_goal_privacy_change_stops_inflight_brief(database: Database) -> None:
    import asyncio

    owner, _, message = await seed(database, "L1")
    goal = await private_goal(database, owner, message)
    source = uuid7()
    async with database.sessions.begin() as session:
        session.add(
            DailyBriefRecord(
                id=source,
                user_id=owner,
                brief_date=NOW.date(),
                status="pending",
                text="fixture body",
                facts=[{"kind": "goal", "text": "fixture body", "source": f"goal:{goal.id}"}],
            )
        )
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def dispatch() -> list[str]:
        started.set()
        try:
            await asyncio.sleep(20)
        finally:
            cancelled.set()
        return ["web_chat"]

    firing = asyncio.create_task(
        deliver_once(
            database,
            table=DailyBriefRecord,
            source_id=source,
            user_id=owner,
            text="fixture body",
            entry="brief.delivery",
            config=RunBudgetConfig(),
            dispatch=dispatch,
        )
    )
    await asyncio.wait_for(started.wait(), 3)
    async with database.sessions.begin() as session:
        row = await session.get(MessageRecord, message)
        assert row is not None
        row.privacy_level = "L2"
    with pytest.raises(BudgetDenied, match="delivery_source_private_or_deleted"):
        await asyncio.wait_for(firing, 3)
    assert cancelled.is_set()
    async with database.sessions() as session:
        root = await session.get(TaskRunRecord, source)
        assert root is not None and root.contract["dispatch_state"] == "unknown"


async def test_private_extraction_preserves_requested_level_above_public_source(
    database: Database,
) -> None:
    from test_goal_tracking import FakeBackend

    from app.cognition import GoalTracker

    owner, _, message = await seed(database, "L1")
    store = CognitiveStore(database)
    backend = FakeBackend(
        '{"commitments":[{"title":"private extraction","due_at":null,"confidence":1}]}'
    )
    values = await GoalTracker(store).ingest_message(
        user_id=owner,
        message_id=message,
        text="private synthetic source",
        privacy_level=PrivacyLevel.L2,
        backend=backend,
    )
    assert len(values) == 1 and values[0].privacy_level == "L2"
    assert await store.active_goals(owner, now=NOW, max_privacy_level=PrivacyLevel.L1) == []


async def test_source_scope_cannot_be_lowered_after_goal_creation(database: Database) -> None:
    owner, _, message = await seed(database, "L2")
    goal = await private_goal(database, owner, message)
    async with database.sessions.begin() as session:
        row = await session.get(MessageRecord, message)
        assert row is not None
        row.privacy_level = "L1"
    store = CognitiveStore(database)
    assert await store.active_goals(owner, now=NOW, max_privacy_level=PrivacyLevel.L1) == []
    # Explicitly requesting a lower level for the same evidence cannot erase
    # the already captured private classification or create a second goal.
    same = await store.create_goal(
        user_id=owner,
        kind=GoalKind.USER,
        title="replacement ignored",
        source_kind="message",
        source_id=str(message),
        privacy_level=PrivacyLevel.L1,
        now=NOW,
    )
    assert same.id == goal.id and same.privacy_level == "L2"
