"""Rejected stability checks still bind durable audit identity to the whole event."""

from uuid import UUID

import pytest
from sqlalchemy import func, select, update
from test_cognitive_save_guard import database as save_database
from test_job_lifecycle_fence import during_commit
from test_perception import create_pipeline, event
from test_perception import user_id as perception_user

from app.cognition import SemanticEvent
from app.db import (
    AppUserRecord,
    CognitiveDecisionRecord,
    Database,
    JobRecord,
    SemanticEventAuditRecord,
)
from app.harness.budget import BudgetDenied
from app.perception import PerceptionDisposition, PerceptionResult, PerceptionStore

database = save_database
user_id = perception_user


@pytest.mark.parametrize("field", ["summary", "attributes"])
async def test_unstable_audit_binds_full_event_before_any_future_replay(
    database: Database, user_id: UUID, field: str
) -> None:
    pipeline = create_pipeline(database)
    source = event(user_id, "unknown_probe")
    handled: list[PerceptionResult] = []

    async def handler(value: SemanticEvent, result: PerceptionResult) -> None:
        handled.append(result)

    pipeline.submit(source, validate=lambda: False, handler=handler)
    for task in tuple(pipeline._tasks.values()):
        await task
    assert len(handled) == 1 and handled[0].disposition == PerceptionDisposition.UNSTABLE
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(CognitiveDecisionRecord)) == 0
        jobs = list(await session.scalars(select(JobRecord)))
        assert len(jobs) == 1 and jobs[0].status == "succeeded" and jobs[0].max_attempts == 1
        assert source.summary not in str(jobs[0].input)
    changed = source.model_copy(
        update={"summary": "changed private source"}
        if field == "summary"
        else {"attributes": {"changed": True}}
    )
    with pytest.raises(BudgetDenied, match="event_source_changed"):
        await pipeline.process(changed)
    assert (await pipeline.process(source)).reason_code == "event_already_processed"


async def test_audit_write_rejects_owner_revoked_while_waiting_for_commit(
    database: Database, user_id: UUID
) -> None:
    source = event(user_id, "unknown_probe")

    async def follow() -> None:
        with pytest.raises(BudgetDenied, match="event_owner_inactive"):
            await PerceptionStore(database).record(
                source,
                dedupe_key="fixture",
                disposition=PerceptionDisposition.UNSTABLE,
                now=source.occurred_at,
            )

    await during_commit(
        database,
        lambda session: session.execute(
            update(AppUserRecord).where(AppUserRecord.id == user_id).values(status="disabled")
        ),
        follow,
    )
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(SemanticEventAuditRecord)) == 0


async def test_unstable_audit_does_not_merge_a_later_valid_distinct_event(
    database: Database, user_id: UUID
) -> None:
    pipeline = create_pipeline(database)
    first = event(user_id, "unknown_probe", dedupe_key="fixture:stability")
    pipeline.submit(first, validate=lambda: False)
    for task in tuple(pipeline._tasks.values()):
        await task
    second = event(user_id, first.kind, dedupe_key=first.dedupe_key)
    result = await pipeline.process(second)
    assert result.disposition == PerceptionDisposition.PROCESSED
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(SemanticEventAuditRecord)) == 2
        assert await session.scalar(select(func.count()).select_from(JobRecord)) == 2
