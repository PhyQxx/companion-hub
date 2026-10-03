"""Replace storage/provider composition without bypassing ingress semantics."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import TypeVar
from uuid import UUID

import pytest
from test_perception import database as perception_database
from test_perception import event
from test_perception import user_id as perception_user_id

from app.cognition.attention import AttentionEngine
from app.cognition.cycle import CognitiveCycle
from app.cognition.models import AttentionResult, CognitiveDecision, SemanticEvent, WorldState
from app.db import Database
from app.harness.budget import BudgetDenied
from app.ids import uuid7
from app.perception.models import PerceptionDisposition, PerceptionResult, ProactivePolicySettings
from app.perception.pipeline import PerceptionPipeline
from app.perception.ports import EventAuditSnapshot
from app.perception.store import PerceptionStore
from app.schemas import PrivacyLevel

# Real SQL adapter tests reuse fixtures; the application-flow tests use no database.
database = perception_database
user_id = perception_user_id
T = TypeVar("T")


class MemoryDecisions:
    def __init__(self) -> None:
        self.saved: list[tuple[CognitiveDecision, SemanticEvent | None, WorldState | None]] = []

    async def save_decision(
        self,
        decision: CognitiveDecision,
        *,
        event: SemanticEvent | None = None,
        state: WorldState | None = None,
    ) -> None:
        self.saved.append((decision, event, state))


class MemoryWorld:
    def __init__(self) -> None:
        self.events: list[SemanticEvent] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def build(self, event: SemanticEvent, *, now: datetime | None = None) -> WorldState:
        self.events.append(event)
        self.entered.set()
        await self.release.wait()
        return WorldState(built_at=now or datetime.now(UTC), timezone="UTC")


class NoProvider:
    async def deliberate(
        self, event: SemanticEvent, state: WorldState, attention: AttentionResult
    ) -> CognitiveDecision:
        raise AssertionError("empty-world probe must not enter a provider")


class MemoryAudits:
    def __init__(self) -> None:
        self.rows: dict[UUID, EventAuditSnapshot] = {}
        self.dispositions: list[PerceptionDisposition] = []
        self.reads: list[UUID] = []

    async def get(self, event_id: UUID) -> EventAuditSnapshot | None:
        self.reads.append(event_id)
        return self.rows.get(event_id)

    async def recent_duplicate(
        self,
        event: SemanticEvent,
        *,
        dedupe_key: str,
        now: datetime,
        window_seconds: int,
    ) -> EventAuditSnapshot | None:
        self.reads.append(event.event_id)
        return None

    async def record(
        self,
        event: SemanticEvent,
        *,
        dedupe_key: str,
        disposition: PerceptionDisposition,
        now: datetime,
        reason_code: str | None = None,
        decision_id: UUID | None = None,
        merged_into_event_id: UUID | None = None,
    ) -> None:
        self.dispositions.append(disposition)
        self.rows[event.event_id] = EventAuditSnapshot(
            event.event_id,
            event.user_id,
            event.kind,
            event.source_kind,
            dedupe_key,
            str(event.privacy_level),
            tuple(event.evidence_ids),
            event.occurred_at,
            event.expires_at,
        )


class MemoryPolicy:
    settings = ProactivePolicySettings(quiet_hours_start="00:00", quiet_hours_end="00:00")

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason

    async def reject_reason(self, event: SemanticEvent, *, now: datetime) -> str | None:
        return self.reason


class MemoryAdmission:
    def __init__(self, *, denied: bool = False) -> None:
        self.denied = denied
        self.verified: list[UUID] = []
        self.started: list[UUID] = []

    async def verify(self, event: SemanticEvent) -> None:
        self.verified.append(event.event_id)

    async def execute(self, event: SemanticEvent, invoke: Callable[[], Awaitable[T]]) -> T:
        if self.denied:
            raise BudgetDenied("event_source_already_admitted")
        self.started.append(event.event_id)
        return await invoke()


def memory_pipeline(
    *, reason: str | None = None, denied: bool = False
) -> tuple[PerceptionPipeline, MemoryDecisions, MemoryWorld, MemoryAudits, MemoryAdmission]:
    decisions, world, audits, admission = (
        MemoryDecisions(),
        MemoryWorld(),
        MemoryAudits(),
        MemoryAdmission(denied=denied),
    )
    pipeline = PerceptionPipeline(
        CognitiveCycle(decisions, world, AttentionEngine(), NoProvider()),
        audits,
        MemoryPolicy(reason),
        admission=admission,
    )
    return pipeline, decisions, world, audits, admission


@pytest.mark.parametrize("privacy", [PrivacyLevel.L1, PrivacyLevel.L3])
async def test_direct_cycle_snapshots_input_before_world_wait(privacy: PrivacyLevel) -> None:
    decisions, world = MemoryDecisions(), MemoryWorld()
    world.release.clear()
    source = event(uuid7(), "unknown_probe", privacy_level=privacy)
    expected = source.model_copy(deep=True)
    task = asyncio.create_task(
        CognitiveCycle(decisions, world, AttentionEngine(), NoProvider()).evaluate(source)
    )
    await asyncio.wait_for(world.entered.wait(), 1)
    source.evidence_ids.append("mutated")
    source.attributes["changed"] = True
    world.release.set()
    decision = await task
    assert decision.user_id == expected.user_id
    assert decision.evidence_ids == expected.evidence_ids
    assert world.events[0] == expected
    assert len(decisions.saved) == (0 if privacy == PrivacyLevel.L3 else 1)


async def test_memory_flow_replays_frozen_audit_without_reexecuting_or_observing() -> None:
    pipeline, decisions, world, audits, admission = memory_pipeline()
    source = event(uuid7(), "unknown_probe")
    observed: list[UUID] = []

    async def observer(value: SemanticEvent) -> None:
        observed.append(value.event_id)

    pipeline.set_event_observer(observer)
    first, replay = await pipeline.process(source), await pipeline.process(source)
    assert first.disposition == PerceptionDisposition.PROCESSED
    assert replay.reason_code == "event_already_processed"
    assert len(decisions.saved) == len(world.events) == len(audits.rows) == 1
    assert admission.started == observed == [source.event_id]
    assert admission.verified == [source.event_id, source.event_id]


@pytest.mark.parametrize("change", ["owner", "source", "evidence"])
async def test_memory_audit_binding_rejects_changed_replay(change: str) -> None:
    pipeline, decisions, _, _, admission = memory_pipeline()
    source = event(uuid7(), "unknown_probe")
    await pipeline.process(source)
    if change == "owner":
        source = source.model_copy(update={"user_id": uuid7()})
    elif change == "source":
        source = source.model_copy(update={"source_kind": "changed"})
    else:
        source.evidence_ids.append("changed")
    with pytest.raises(BudgetDenied, match="event_source_"):
        await pipeline.process(source)
    assert len(decisions.saved) == len(admission.started) == 1


async def test_private_flow_never_admits_persists_or_observes() -> None:
    pipeline, decisions, world, audits, admission = memory_pipeline()
    observed: list[UUID] = []

    async def observer(value: SemanticEvent) -> None:
        observed.append(value.event_id)

    pipeline.set_event_observer(observer)
    pipeline.set_departure_observer(observer)
    result = await pipeline.process(event(uuid7(), "unknown_probe", privacy_level=PrivacyLevel.L3))
    assert result.disposition == PerceptionDisposition.PROCESSED
    assert len(world.events) == 1
    assert not decisions.saved and not audits.rows and not observed
    assert not audits.reads
    assert not admission.verified and not admission.started


@pytest.mark.parametrize("private_first", [True, False])
async def test_private_and_durable_events_do_not_share_dedupe(private_first: bool) -> None:
    pipeline, decisions, world, audits, admission = memory_pipeline()
    public = event(uuid7(), "unknown_probe", dedupe_key="same-source")
    private = public.model_copy(update={"privacy_level": PrivacyLevel.L3})
    sources = (private, public) if private_first else (public, private)
    results = [await pipeline.process(value) for value in sources]
    assert all(value.disposition == PerceptionDisposition.PROCESSED for value in results)
    assert len(decisions.saved) == len(audits.rows) == len(admission.started) == 1
    assert len(world.events) == 2


@pytest.mark.parametrize("mode", ["expired", "policy", "unstable"])
async def test_private_rejections_do_not_touch_audit_ports(mode: str) -> None:
    pipeline, decisions, world, audits, admission = memory_pipeline(
        reason="dnd" if mode == "policy" else None
    )
    source = event(uuid7(), "unknown_probe", privacy_level=PrivacyLevel.L3)
    if mode == "expired":
        source = source.model_copy(update={"expires_at": source.occurred_at})
    if mode == "unstable":
        completed = asyncio.Event()

        async def handled(value: SemanticEvent, result: PerceptionResult) -> None:
            assert result.disposition == PerceptionDisposition.UNSTABLE
            completed.set()

        pipeline.submit(source, validate=lambda: False, handler=handled)
        await asyncio.wait_for(completed.wait(), 1)
        await pipeline.stop()
    else:
        result = await pipeline.process(source)
        assert result.reason_code == ("dnd" if mode == "policy" else "event_expired")
    assert not decisions.saved and not world.events
    assert not audits.rows and not audits.reads
    assert not admission.verified and not admission.started


@pytest.mark.parametrize("mode", ["policy", "expired", "admission"])
async def test_replaced_ports_preserve_rejection_order(mode: str) -> None:
    pipeline, decisions, world, audits, admission = memory_pipeline(
        reason="dnd" if mode == "policy" else None, denied=mode == "admission"
    )
    source = event(uuid7(), "unknown_probe")
    if mode == "expired":
        source = source.model_copy(update={"expires_at": source.occurred_at})
    if mode == "admission":
        with pytest.raises(BudgetDenied, match="event_source_already_admitted"):
            await pipeline.process(source)
        assert not decisions.saved and not audits.rows and not admission.started
    else:
        result = await pipeline.process(source)
        assert result.reason_code == ("dnd" if mode == "policy" else "event_expired")
        assert len(decisions.saved) == (1 if mode == "policy" else 0)
        assert len(audits.rows) == len(admission.started) == 1
    assert not world.events


async def test_delayed_submission_uses_original_event_snapshot() -> None:
    pipeline, _, world, _, _ = memory_pipeline()
    source = event(uuid7(), "unknown_probe")
    expected = source.model_copy(deep=True)
    completed = asyncio.Event()
    handled: list[SemanticEvent] = []

    async def handler(value: SemanticEvent, result: PerceptionResult) -> None:
        handled.append(value)
        completed.set()

    pipeline.submit(source, stable_for_seconds=0.01, handler=handler)
    source.evidence_ids.append("late_mutation")
    source.attributes["late_content"] = "changed"
    await asyncio.wait_for(completed.wait(), 1)
    await pipeline.stop()
    assert handled == world.events == [expected]


async def test_sql_adapter_returns_frozen_metadata_from_both_reads(
    database: Database, user_id: UUID
) -> None:
    store = PerceptionStore(database)
    source = event(user_id, "unknown_probe", dedupe_key="fixture:immutable")
    await store.record(
        source,
        dedupe_key="fixture:immutable",
        disposition=PerceptionDisposition.PROCESSED,
        now=datetime.now(UTC),
    )
    snapshot = await store.get(source.event_id)
    duplicate = await store.recent_duplicate(
        source, dedupe_key="fixture:immutable", now=datetime.now(UTC), window_seconds=300
    )
    assert isinstance(snapshot, EventAuditSnapshot)
    assert duplicate == snapshot
    assert snapshot.evidence_ids == tuple(source.evidence_ids)
    source.evidence_ids.append("caller_change")
    assert snapshot.evidence_ids != tuple(source.evidence_ids)
    with pytest.raises(FrozenInstanceError):
        snapshot.user_id = uuid7()  # type: ignore[misc] # Verify the runtime freeze too.
    assert await store.get(source.event_id) == snapshot
