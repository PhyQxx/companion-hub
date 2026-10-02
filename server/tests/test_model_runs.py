from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select
from test_database_config import config_yaml
from test_llm import FakeProvider

from app.config import ConfigSnapshot, ConfigStore
from app.db import (
    AppUserRecord,
    Base,
    Database,
    ModelReservationRecord,
    TaskRunRecord,
    create_database,
)
from app.harness.budget import BudgetDenied, current_budget
from app.ids import uuid7
from app.llm.contracts import CompletionRequest, CompletionResult, LLMMessage, LLMRoute
from app.llm.router import LLMRouteExhausted, LLMRouter
from app.runs.completion import complete_with_run
from app.runs.store import RunStore


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    value = create_database(f"sqlite+aiosqlite:///{tmp_path / 'model-runs.db'}")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield value
    finally:
        await value.close()


async def setup(
    database: Database, tmp_path: Path
) -> tuple[UUID, ConfigSnapshot, CompletionRequest]:
    owner = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=owner, display_name="Fixture", status="active"))
    path = tmp_path / "fixture.yaml"
    path.write_text(config_yaml() + "\nrun_budget:\n  max_llm_attempts: 2\n", encoding="utf-8")
    snapshot = await ConfigStore(path).load()
    request = CompletionRequest(
        trace_id=uuid7(),
        privacy_level="L2",
        route=LLMRoute.PRIVATE,
        messages=[LLMMessage(role="user", content="private fixture never in run metadata")],
    )
    return owner, snapshot, request


def router(snapshot: ConfigSnapshot, cloud: FakeProvider, local: FakeProvider) -> LLMRouter:
    return LLMRouter(
        endpoints=snapshot.config.models,
        routes=snapshot.config.routes,
        providers={"cloud": cloud, "local": local},
    )


async def test_model_only_run_is_budgeted_local_and_not_semantically_verified(
    database: Database, tmp_path: Path
) -> None:
    owner, snapshot, request = await setup(database, tmp_path)
    cloud, local = FakeProvider("cloud"), FakeProvider("local")
    backend = router(snapshot, cloud, local)
    source_id = uuid7()
    result = await complete_with_run(
        database,
        snapshot,
        request,
        backend.complete,
        user_id=owner,
        kind="fixture.model",
        source_id=source_id,
    )
    assert result.text == "ok" and not cloud.requests and len(local.requests) == 1
    assert current_budget() is None
    runs = await RunStore(database).list_runs(user_id=owner)
    assert len(runs) == 1 and runs[0].privacy_level == "L2"
    view = await RunStore(database).get(runs[0].id, user_id=owner)
    assert view.status == "succeeded" and view.budget_summary is not None
    assert view.budget_summary.llm_attempts == 1
    assert view.goal is not None and view.goal.status == "inconclusive"
    async with database.sessions() as session:
        record = await session.get(TaskRunRecord, view.id)
        assert record is not None
        assert "private fixture" not in str(record.contract)
    with pytest.raises(BudgetDenied, match="source_already_processed"):
        await complete_with_run(
            database,
            snapshot,
            request,
            backend.complete,
            user_id=owner,
            kind="fixture.model",
            source_id=source_id,
        )
    assert len(local.requests) == 1


async def test_cancelled_model_run_does_not_return_a_late_completion(
    database: Database, tmp_path: Path
) -> None:
    owner, snapshot, request = await setup(database, tmp_path)
    started, release = asyncio.Event(), asyncio.Event()

    class WaitingProvider(FakeProvider):
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            started.set()
            await release.wait()
            return await super().complete(request)

    local = WaitingProvider("local")
    backend = router(snapshot, FakeProvider("cloud"), local)
    task = asyncio.create_task(
        complete_with_run(
            database,
            snapshot,
            request,
            backend.complete,
            user_id=owner,
            kind="fixture.model",
            source_id=uuid7(),
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        view = (await RunStore(database).list_runs(user_id=owner))[0]
        assert (await RunStore(database).cancel_work(view.id, user_id=owner))[0]
        release.set()
        with pytest.raises(BudgetDenied, match="budget_run_inactive"):
            await task
        assert (await RunStore(database).get(view.id, user_id=owner)).status == "cancelled"
        assert current_budget() is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("reason", ["owner", "source", "expired", "ephemeral"])
async def test_model_run_denies_invalid_context_before_calling_provider(
    database: Database, tmp_path: Path, reason: str
) -> None:
    owner, snapshot, request = await setup(database, tmp_path)
    cloud, local = FakeProvider("cloud"), FakeProvider("local")
    backend = router(snapshot, cloud, local)
    if reason == "ephemeral":
        request = request.model_copy(update={"privacy_level": "L3"})
    with pytest.raises(BudgetDenied):
        await complete_with_run(
            database,
            snapshot,
            request,
            backend.complete,
            user_id=uuid7() if reason == "owner" else owner,
            kind="fixture.model",
            source_id=uuid7(),
            conversation_id=uuid7() if reason == "source" else None,
            expires_at=datetime.now(UTC) - timedelta(seconds=1) if reason == "expired" else None,
        )
    assert not cloud.requests and not local.requests
    assert await RunStore(database).list_runs(user_id=owner) == []


async def test_failed_model_only_run_keeps_unknown_reservations(
    database: Database, tmp_path: Path
) -> None:
    owner, snapshot, request = await setup(database, tmp_path)
    cloud, local = FakeProvider("cloud"), FakeProvider("local", fail=True)
    backend = router(snapshot, cloud, local)
    with pytest.raises(LLMRouteExhausted):
        await complete_with_run(
            database,
            snapshot,
            request,
            backend.complete,
            user_id=owner,
            kind="fixture.model",
            source_id=uuid7(),
        )
    view = (await RunStore(database).list_runs(user_id=owner))[0]
    assert view.status == "failed"
    async with database.sessions() as session:
        reservations = list(await session.scalars(select(ModelReservationRecord)))
        assert reservations and all(item.state == "unknown" for item in reservations)
    assert current_budget() is None


async def test_recovery_expires_interrupted_model_runs_without_releasing_unknown_charge(
    database: Database, tmp_path: Path
) -> None:
    from app.config.models import RunBudgetConfig
    from app.runs.budget import RunModelBudget
    from app.runs.completion import recover_expired_model_runs

    owner, _snapshot, _ = await setup(database, tmp_path)
    now = datetime.now(UTC)
    expired_id, live_id = uuid7(), uuid7()
    config = RunBudgetConfig()
    async with database.sessions.begin() as session:
        session.add_all(
            [
                TaskRunRecord(
                    id=run_id,
                    user_id=owner,
                    status="running",
                    privacy_level="L1",
                    contract={"criterion": "model_result_returned", "required_work": []},
                    budget=config.model_dump(mode="json"),
                    deadline=now + timedelta(minutes=1),
                    created_at=now,
                    updated_at=now,
                )
                for run_id in (expired_id, live_id)
            ]
        )
    budget = RunModelBudget(database, run_id=expired_id, user_id=owner, config=config)
    permit = await budget.reserve(endpoint="fixture", tokens=20, final=True)
    async with database.sessions.begin() as session:
        expired = await session.get(TaskRunRecord, expired_id)
        assert expired is not None
        expired.deadline = now - timedelta(seconds=1)
    assert await recover_expired_model_runs(database) == 1
    assert await recover_expired_model_runs(database) == 0
    async with database.sessions() as session:
        expired = await session.get(TaskRunRecord, expired_id)
        live = await session.get(TaskRunRecord, live_id)
        reservation = await session.get(ModelReservationRecord, permit.call_id)
        assert expired is not None and expired.status == "failed" and expired.budget_tokens == 20
        assert live is not None and live.status == "running"
        assert reservation is not None and reservation.state == "unknown"


@pytest.mark.parametrize("kind", ["mail", "browser", "meeting"])
async def test_analyzer_ports_require_owned_scope_and_record_model_runs(
    database: Database, tmp_path: Path, kind: str
) -> None:
    from app.browser_awareness import LlmBrowserAnalyzer
    from app.mail_awareness import LlmMailAnalyzer
    from app.meetings import LlmMeetingSummarizer
    from app.meetings.models import TranscriptSegment
    from app.runs.completion import model_owner
    from app.schemas import PrivacyLevel

    owner, snapshot, _ = await setup(database, tmp_path)
    config = ConfigStore(tmp_path / "fixture.yaml")
    await config.load()

    class JsonProvider(FakeProvider):
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            result = await super().complete(request)
            return result.model_copy(
                update={
                    "text": (
                        '{"summary":"synthetic","notable":false,"memory_worthy":false,'
                        '"decisions":[],"action_items":[]}'
                    )
                }
            )

    cloud, local = JsonProvider("cloud"), JsonProvider("local")
    backend = router(snapshot, cloud, local)
    source_id = uuid7()

    async def analyze() -> object:
        if kind == "mail":
            return await LlmMailAnalyzer(
                config, database=database, router_builder=lambda cfg: backend
            ).analyze(
                observation_id=source_id,
                sender="synthetic",
                subject="synthetic",
                snippet="synthetic",
                prompt="synthetic",
            )
        if kind == "browser":
            return await LlmBrowserAnalyzer(
                config, database=database, router_builder=lambda cfg: backend
            ).analyze(
                observation_id=source_id,
                title="synthetic",
                origin="https://example.invalid",
                text="synthetic",
                prompt="synthetic",
            )
        return await LlmMeetingSummarizer(
            config, database=database, router_builder=lambda cfg: backend
        ).summarize(
            meeting_id=source_id,
            title="synthetic",
            segments=[TranscriptSegment(speaker="fixture", text="synthetic")],
            privacy_level=PrivacyLevel.L2,
        )

    with pytest.raises(BudgetDenied, match="model_run_owner_missing"):
        await analyze()
    with model_owner(owner):
        await analyze()
    views = await RunStore(database).list_runs(user_id=owner)
    assert len(views) == 1 and views[0].budget_summary is not None
    assert views[0].budget_summary.llm_attempts == 1 and views[0].status == "succeeded"
    if kind == "meeting":
        assert not cloud.requests and len(local.requests) == 1
        assert views[0].privacy_level == "L2"
    else:
        assert len(cloud.requests) == 1 and not local.requests
    # Scope has been reset even after returning through the legacy port.
    with pytest.raises(BudgetDenied, match="model_run_owner_missing"):
        await analyze()


async def test_nested_model_call_reuses_owned_budget_and_rejects_another_owner(
    database: Database, tmp_path: Path
) -> None:
    from app.config.models import RunBudgetConfig
    from app.harness.budget import budget_scope
    from app.runs.budget import RunModelBudget

    owner, snapshot, request = await setup(database, tmp_path)
    root_id = uuid7()
    now = datetime.now(UTC)
    config = RunBudgetConfig()
    async with database.sessions.begin() as session:
        session.add(
            TaskRunRecord(
                id=root_id,
                user_id=owner,
                status="running",
                privacy_level="L2",
                contract={"criterion": "reply_committed"},
                budget=config.model_dump(mode="json"),
                deadline=now + timedelta(minutes=5),
                created_at=now,
                updated_at=now,
            )
        )
    budget = RunModelBudget(database, run_id=root_id, user_id=owner, config=config)
    cloud, local = FakeProvider("cloud"), FakeProvider("local")
    backend = router(snapshot, cloud, local)
    with budget_scope(budget):
        with pytest.raises(BudgetDenied, match="budget_owner_invalid"):
            await complete_with_run(
                database,
                snapshot,
                request,
                backend.complete,
                user_id=uuid7(),
                kind="fixture.nested",
                source_id=uuid7(),
            )
        await complete_with_run(
            database,
            snapshot,
            request,
            backend.complete,
            user_id=owner,
            kind="fixture.nested",
            source_id=uuid7(),
        )
    views = await RunStore(database).list_runs(user_id=owner)
    assert len(views) == 1 and views[0].id == root_id and views[0].budget_summary is not None
    assert views[0].budget_summary.llm_attempts == 1 and len(local.requests) == 1
