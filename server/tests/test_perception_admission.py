"""Independent pipelines share durable admission and retain enclosing fences."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select, update
from test_cognitive_save_guard import database as save_database
from test_perception import create_pipeline, event
from test_perception import user_id as perception_user

from app.db import CognitiveDecisionRecord, Database, JobRecord, SemanticEventAuditRecord
from app.harness.budget import BudgetDenied
from app.harness.claim import ClaimInvalidated, ExecutionClaim, claim_scope, current_claims
from app.jobs.engine import JobEngine
from app.perception import PerceptionDisposition, PerceptionStore
from app.perception.admission import EventAdmission

database = save_database
user_id = perception_user


@pytest.mark.parametrize("duplicate", ["same_event", "cross_source"])
async def test_independent_pipelines_admit_one_decision(
    database: Database, user_id: UUID, duplicate: str
) -> None:
    first = event(user_id, "user_arrived_home", dedupe_key="fixture:shared")
    pipelines = [create_pipeline(database) for _ in range(8)]
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    observed: list[UUID] = []

    async def observer(value: Any) -> None:
        observed.append(value.event_id)

    for pipeline in pipelines:
        cycle = pipeline._cycle

        async def evaluate(value: Any, original: Any = cycle.evaluate) -> Any:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return await original(value)

        pipeline._cycle = SimpleNamespace(evaluate=evaluate, suppress=cycle.suppress)
        pipeline.set_event_observer(observer)
    task = asyncio.create_task(pipelines[0].process(first))
    await asyncio.wait_for(entered.wait(), 5)
    sources = [
        first
        if duplicate == "same_event"
        else event(
            user_id,
            "user_arrived_home",
            source_kind=f"fixture-{index}",
            dedupe_key="fixture:shared",
        )
        for index in range(7)
    ]
    losers = await asyncio.gather(
        *(pipeline.process(value) for pipeline, value in zip(pipelines[1:], sources, strict=True)),
        return_exceptions=True,
    )
    release.set()
    result = await asyncio.wait_for(task, 5)
    assert result.disposition == PerceptionDisposition.PROCESSED and calls == 1
    assert all(isinstance(value, BudgetDenied) for value in losers)
    assert observed == [first.event_id]
    assert (await pipelines[1].process(first)).reason_code == "event_already_processed"
    if duplicate == "cross_source":
        assert (await pipelines[1].process(sources[0])).reason_code == "cross_source_duplicate"
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(CognitiveDecisionRecord)) == 1
        jobs = list(await session.scalars(select(JobRecord)))
    assert all(job.max_attempts == 1 and job.status == "succeeded" for job in jobs)
    assert first.summary not in json.dumps([job.input for job in jobs])
    assert "fixture:shared" not in json.dumps([job.input for job in jobs])


@pytest.mark.parametrize(
    "field", ["summary", "attributes", "conversation", "confidence", "passive"]
)
async def test_replay_binds_full_semantic_fingerprint(
    database: Database, user_id: UUID, field: str
) -> None:
    pipeline = create_pipeline(database)
    source = event(user_id, "user_arrived_home")
    await pipeline.process(source)
    changes: dict[str, Any] = {
        "summary": {"summary": "unrelated private content"},
        "attributes": {"attributes": {"device_id": "unrelated"}},
        "conversation": {"conversation_id": UUID(int=1)},
        "confidence": {"confidence": 0.1},
        "passive": {"passive": True},
    }
    with pytest.raises(BudgetDenied, match="event_source_changed"):
        await create_pipeline(database).process(source.model_copy(update=changes[field]))
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(CognitiveDecisionRecord)) == 1


async def test_interrupted_source_and_cross_source_unknown_are_not_reexecuted(
    database: Database, user_id: UUID
) -> None:
    source = event(user_id, "user_arrived_home", dedupe_key="fixture:unknown")
    pipeline = create_pipeline(database)
    calls = 0

    async def interrupted(value: Any) -> Any:
        nonlocal calls
        calls += 1
        raise RuntimeError("private fixture interruption")

    pipeline._cycle = SimpleNamespace(evaluate=interrupted)
    with pytest.raises(RuntimeError, match="private fixture interruption"):
        await pipeline.process(source)
    with pytest.raises(BudgetDenied, match="event_source_already_admitted"):
        await create_pipeline(database).process(source)
    with pytest.raises(BudgetDenied, match="event_dedupe_in_flight_or_unknown"):
        await create_pipeline(database).process(
            event(user_id, source.kind, dedupe_key=source.dedupe_key)
        )
    async with database.sessions() as session:
        job = await session.scalar(select(JobRecord))
        assert job is not None and job.status == "cancelled" and job.attempts == 1
        assert await session.scalar(select(func.count()).select_from(CognitiveDecisionRecord)) == 0
    assert calls == 1


@pytest.mark.parametrize("revocation", ["child_cancel", "child_expiry", "parent_cancel"])
async def test_admitted_event_rejects_late_result_when_any_claim_is_lost(
    database: Database, user_id: UUID, revocation: str
) -> None:
    engine = JobEngine(database)
    parent = await engine.submit("fixture.parent", {}, owner=str(user_id), resource_class="fixture")
    claimed = await engine.claim("parent-worker", resource_class="fixture", job_id=parent.id)
    assert claimed is not None
    pipeline = create_pipeline(database)
    original = pipeline._cycle
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def evaluate(value: Any) -> Any:
        assert len(current_claims()) == 2
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            # A cancellation-defying port still cannot save its late decision.
            return await original.evaluate(value)

    pipeline._cycle = SimpleNamespace(evaluate=evaluate, suppress=original.suppress)
    with claim_scope(ExecutionClaim(parent.id, "parent-worker", claimed.attempts)):
        task = asyncio.create_task(pipeline.process(event(user_id, "user_arrived_home")))
    await asyncio.wait_for(entered.wait(), 5)
    async with database.sessions() as session:
        child = await session.scalar(select(JobRecord).where(JobRecord.kind == "perception.event"))
    assert child is not None
    if revocation == "child_expiry":
        async with database.sessions.begin() as session:
            await session.execute(
                update(JobRecord)
                .where(JobRecord.id == child.id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
    else:
        await engine.cancel(parent.id if revocation == "parent_cancel" else child.id)
    with pytest.raises(ClaimInvalidated):
        await asyncio.wait_for(task, 5)
    assert cancelled.is_set()
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(CognitiveDecisionRecord)) == 0
        assert await session.scalar(select(func.count()).select_from(SemanticEventAuditRecord)) == 0
    assert not current_claims()


@pytest.mark.parametrize("revoked", ["parent", "child"])
async def test_nested_claims_fence_the_same_derived_transaction(
    database: Database, user_id: UUID, revoked: str
) -> None:
    engine = JobEngine(database)
    claims = []
    for name in ("parent", "child"):
        job = await engine.submit(
            f"fixture.{name}", {}, owner=str(user_id), resource_class="fixture"
        )
        claimed = await engine.claim(name, resource_class="fixture", job_id=job.id)
        assert claimed is not None
        claims.append(ExecutionClaim(job.id, name, claimed.attempts))
    with claim_scope(claims[0]):
        with claim_scope(claims[1]):
            assert [claim.job_id for claim in current_claims()] == [
                claim.job_id for claim in claims
            ]
            await engine.cancel(claims[0 if revoked == "parent" else 1].job_id)
            with pytest.raises(ClaimInvalidated):
                await PerceptionStore(database).record(
                    event(user_id, "user_arrived_home"),
                    dedupe_key="fixture",
                    now=datetime.now(UTC),
                    disposition=PerceptionDisposition.UNSTABLE,
                )
        assert current_claims() == (claims[0],)
    assert not current_claims()


async def test_replay_fingerprint_normalizes_timestamps_and_attribute_key_order(
    database: Database, user_id: UUID
) -> None:
    from datetime import timezone

    source = event(user_id, "user_arrived_home").model_copy(update={"attributes": {"a": 1, "b": 2}})
    await create_pipeline(database).process(source)
    equivalent = source.model_copy(
        update={
            "occurred_at": source.occurred_at.astimezone(timezone(timedelta(hours=8))),
            "expires_at": source.expires_at.replace(tzinfo=None) if source.expires_at else None,
            "attributes": {"b": 2, "a": 1},
        }
    )
    assert (
        await create_pipeline(database).process(equivalent)
    ).reason_code == "event_already_processed"


async def test_live_claim_blocks_cross_source_after_window_and_expired_claim_cannot_replay(
    database: Database, user_id: UUID
) -> None:
    source = event(user_id, "user_arrived_home", dedupe_key="fixture:crash")
    pipeline = create_pipeline(database, dedupe_window_seconds=1)
    engine = JobEngine(database)
    admission = pipeline._admission
    assert isinstance(admission, EventAdmission)
    job = await admission._admit(source)
    assert await engine.claim("lost-process", resource_class="perception-inline", job_id=job.id)
    async with database.sessions.begin() as session:
        await session.execute(
            update(JobRecord)
            .where(JobRecord.id == job.id)
            .values(created_at=datetime.now(UTC) - timedelta(minutes=10))
        )
    next_source = event(user_id, source.kind, dedupe_key=source.dedupe_key)
    with pytest.raises(BudgetDenied, match="event_dedupe_in_flight_or_unknown"):
        await create_pipeline(database, dedupe_window_seconds=1).process(next_source)
    async with database.sessions.begin() as session:
        await session.execute(
            update(JobRecord)
            .where(JobRecord.id == job.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    with pytest.raises(BudgetDenied, match="event_source_already_admitted"):
        await create_pipeline(database, dedupe_window_seconds=1).process(source)
    result = await create_pipeline(database, dedupe_window_seconds=1).process(next_source)
    assert result.disposition == PerceptionDisposition.PROCESSED
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(CognitiveDecisionRecord)) == 1
