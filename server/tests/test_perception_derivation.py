"""Replayed, ephemeral and revoked event attempts cannot derive durable work."""

import asyncio
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
from app.harness.claim import ClaimInvalidated, ExecutionClaim, claim_scope
from app.ids import uuid7
from app.jobs.engine import JobEngine
from app.perception import PerceptionDisposition, PerceptionStore
from app.schemas import PrivacyLevel

database = save_database
user_id = perception_user


async def test_ordinary_observers_only_receive_the_first_processed_event(
    database: Database, user_id: UUID
) -> None:
    pipeline = create_pipeline(database)
    observed: list[UUID] = []

    async def observer(value: Any) -> None:
        observed.append(value.event_id)

    pipeline.set_event_observer(observer)
    first = event(user_id, "user_arrived_home", dedupe_key="fixture:arrival")
    assert (await pipeline.process(first)).disposition == PerceptionDisposition.PROCESSED
    assert (await pipeline.process(first)).reason_code == "event_already_processed"
    duplicate = event(
        user_id, "user_arrived_home", source_kind="mqtt", dedupe_key="fixture:arrival"
    )
    assert (await pipeline.process(duplicate)).reason_code == "cross_source_duplicate"
    assert observed == [first.event_id]


@pytest.mark.parametrize("kind", ["user_arrived_home", "user_left_home"])
async def test_ephemeral_events_do_not_reach_durable_or_departure_observers(
    database: Database, user_id: UUID, kind: str
) -> None:
    pipeline = create_pipeline(database)
    observed: list[UUID] = []

    async def observer(value: Any) -> None:
        observed.append(value.event_id)

    pipeline.set_event_observer(observer)
    pipeline.set_departure_observer(observer)
    result = await pipeline.process(event(user_id, kind, privacy_level=PrivacyLevel.L3))
    assert result.disposition == PerceptionDisposition.PROCESSED
    assert not observed
    async with database.sessions() as session:
        for model in (CognitiveDecisionRecord, SemanticEventAuditRecord, JobRecord):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize(
    "field", ["owner", "kind", "source", "dedupe", "privacy", "evidence", "occurred", "expires"]
)
async def test_existing_event_id_binds_owned_audited_source_fields(
    database: Database, user_id: UUID, field: str
) -> None:
    pipeline = create_pipeline(database)
    source = event(user_id, "user_arrived_home")
    await pipeline.process(source)
    changes: dict[str, Any] = {
        "owner": {"user_id": uuid7()},
        "kind": {"kind": "user_left_home"},
        "source": {"source_kind": "unrelated"},
        "dedupe": {"dedupe_key": "other"},
        "privacy": {"privacy_level": PrivacyLevel.L0},
        "evidence": {"evidence_ids": ["other"]},
        "occurred": {"occurred_at": source.occurred_at + timedelta(seconds=1)},
        "expires": {"expires_at": None},
    }
    with pytest.raises(BudgetDenied) as rejection:
        await pipeline.process(source.model_copy(update=changes[field]))
    assert rejection.value.reason_code == (
        "event_source_owner_mismatch" if field == "owner" else "event_source_changed"
    )
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(CognitiveDecisionRecord)) == 1
        assert await session.scalar(select(func.count()).select_from(SemanticEventAuditRecord)) == 1


async def test_source_expiring_after_decision_does_not_trigger_an_observer(
    database: Database, user_id: UUID
) -> None:
    pipeline = create_pipeline(database)
    cycle = pipeline._cycle
    observed: list[UUID] = []

    async def observer(value: Any) -> None:
        observed.append(value.event_id)

    async def evaluate(value: Any) -> Any:
        decision = await cycle.evaluate(value)
        await asyncio.sleep(1.1)
        return decision

    pipeline.set_event_observer(observer)
    pipeline._cycle = SimpleNamespace(evaluate=evaluate, suppress=cycle.suppress)
    source = event(
        user_id, "user_arrived_home", expires_at=datetime.now(UTC) + timedelta(seconds=1)
    )
    result = await pipeline.process(source)
    assert result.disposition == PerceptionDisposition.PROCESSED
    assert not observed


@pytest.mark.parametrize("revocation", ["cancel", "expiry"])
async def test_event_audit_is_fenced_by_the_current_background_claim(
    database: Database, user_id: UUID, revocation: str
) -> None:
    engine = JobEngine(database)
    job = await engine.submit(
        "fixture.perception", {}, owner=str(user_id), resource_class="fixture"
    )
    claimed = await engine.claim("fixture-worker", resource_class="fixture", job_id=job.id)
    assert claimed is not None
    if revocation == "cancel":
        await engine.cancel(job.id)
    else:
        async with database.sessions.begin() as session:
            await session.execute(
                update(JobRecord)
                .where(JobRecord.id == job.id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
    with (
        claim_scope(ExecutionClaim(job.id, "fixture-worker", claimed.attempts)),
        pytest.raises(ClaimInvalidated),
    ):
        await PerceptionStore(database).record(
            event(user_id, "user_arrived_home"),
            dedupe_key="fixture",
            now=datetime.now(UTC),
            disposition=PerceptionDisposition.UNSTABLE,
        )
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(SemanticEventAuditRecord)) == 0
