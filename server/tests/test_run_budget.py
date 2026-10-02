from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select, update
from test_llm import FakeProvider, endpoint

from app.config.models import RunBudgetConfig
from app.db import (
    AppUserRecord,
    Base,
    Database,
    ModelReservationRecord,
    TaskRunRecord,
    create_database,
)
from app.harness.budget import BudgetDenied, CallPermit, budget_scope, current_budget
from app.ids import uuid7
from app.llm.contracts import CompletionRequest, LLMMessage, LLMRoute, ModelUsage, RoutePolicy
from app.llm.router import LLMRouter
from app.runs.budget import RunModelBudget, recover_stale_reservations


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    value = create_database(f"sqlite+aiosqlite:///{tmp_path / 'budget.db'}")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield value
    finally:
        await value.close()


async def make_budget(database: Database, config: RunBudgetConfig) -> RunModelBudget:
    run_id, user_id = uuid7(), uuid7()
    now = datetime.now(UTC)
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=user_id, display_name="Budget fixture", status="active"))
        session.add(
            TaskRunRecord(
                id=run_id,
                user_id=user_id,
                contract={},
                budget=config.model_dump(),
                status="running",
                privacy_level="L1",
                state_version=1,
                event_seq=0,
                cancel_epoch=0,
                created_at=now,
                updated_at=now,
                deadline=now + timedelta(seconds=config.interactive_deadline_seconds),
            )
        )
    return RunModelBudget(database, run_id=run_id, user_id=user_id, config=config)


@pytest.mark.parametrize("tokens, attempts, expected", [(700, 8, 1), (10, 2, 2)])
async def test_parallel_admission_cannot_overspend(
    database: Database, tokens: int, attempts: int, expected: int
) -> None:
    budget = await make_budget(
        database, RunBudgetConfig(max_tokens=1024, max_llm_attempts=attempts)
    )
    outcomes = await asyncio.gather(
        *(budget.reserve(endpoint="test", tokens=tokens, final=True) for _ in range(10)),
        return_exceptions=True,
    )
    permits = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    assert len(permits) == expected
    assert all(
        isinstance(outcome, BudgetDenied)
        for outcome in outcomes
        if isinstance(outcome, BaseException)
    )
    async with database.sessions() as session:
        row = await session.get(TaskRunRecord, budget._run_id)
        assert row is not None and row.llm_attempts == expected
        assert row.budget_tokens == tokens * expected
        assert len(list(await session.scalars(select(ModelReservationRecord)))) == expected


async def test_settlement_is_idempotent_and_unknown_usage_survives_restart(
    database: Database,
) -> None:
    config = RunBudgetConfig(max_tokens=1024)
    budget = await make_budget(database, config)
    permit = await budget.reserve(endpoint="test", tokens=700, final=True)
    await budget.settle(permit.call_id, None)
    restarted = RunModelBudget(
        database, run_id=budget._run_id, user_id=budget._user_id, config=config
    )
    with pytest.raises(BudgetDenied, match="run_budget_exhausted"):
        await restarted.reserve(endpoint="test", tokens=700, final=True)
    await asyncio.gather(
        *(restarted.settle(permit.call_id, ModelUsage(total_tokens=80)) for _ in range(6))
    )
    async with database.sessions() as session:
        row = await session.get(TaskRunRecord, budget._run_id)
        assert row is not None and row.budget_tokens == 80 and row.llm_attempts == 1
        record = await session.get(ModelReservationRecord, permit.call_id)
        assert record is not None and record.state == "settled"
    with pytest.raises(BudgetDenied, match="budget_settlement_conflict"):
        await restarted.settle(permit.call_id, ModelUsage(total_tokens=90))
    assert await restarted.reserve(endpoint="test", tokens=700, final=True)


async def test_recovery_preserves_uncertain_charge(database: Database) -> None:
    budget = await make_budget(database, RunBudgetConfig())
    permit = await budget.reserve(endpoint="test", tokens=700, final=True)
    async with database.sessions.begin() as session:
        await session.execute(
            update(ModelReservationRecord)
            .where(ModelReservationRecord.call_id == permit.call_id)
            .values(created_at=datetime.now(UTC) - timedelta(seconds=2000))
        )
    await recover_stale_reservations(database)
    async with database.sessions() as session:
        row = await session.get(TaskRunRecord, budget._run_id)
        record = await session.get(ModelReservationRecord, permit.call_id)
        assert row is not None and row.budget_tokens == 700
        assert record is not None and record.state == "unknown"


async def test_deadline_owner_and_cancellation_fail_before_reservation(database: Database) -> None:
    config = RunBudgetConfig()
    budget = await make_budget(database, config)
    stranger = RunModelBudget(database, run_id=budget._run_id, user_id=uuid7(), config=config)
    with pytest.raises(BudgetDenied, match="budget_run_not_found"):
        await stranger.reserve(endpoint="test", tokens=10, final=True)
    async with database.sessions.begin() as session:
        await session.execute(
            update(TaskRunRecord)
            .where(TaskRunRecord.id == budget._run_id)
            .values(status="cancelled")
        )
    with pytest.raises(BudgetDenied, match="budget_run_inactive"):
        await budget.reserve(endpoint="test", tokens=10, final=True)
    async with database.sessions.begin() as session:
        await session.execute(
            update(TaskRunRecord)
            .where(TaskRunRecord.id == budget._run_id)
            .values(status="running", deadline=datetime.now(UTC) - timedelta(seconds=1))
        )
    with pytest.raises(BudgetDenied, match="run_deadline_exceeded"):
        await budget.reserve(endpoint="test", tokens=10, final=True)
    async with database.sessions() as session:
        assert list(await session.scalars(select(ModelReservationRecord))) == []


async def test_maintenance_shares_parent_quota_and_tool_free_slot_is_preserved(
    database: Database,
) -> None:
    config = RunBudgetConfig(max_llm_attempts=2)
    budget = await make_budget(database, config)
    first = await budget.reserve(endpoint="test", tokens=100, final=False)
    await budget.settle(first.call_id, ModelUsage(total_tokens=80))
    with pytest.raises(BudgetDenied, match="run_budget_exhausted"):
        await budget.reserve(endpoint="test", tokens=100, final=False)
    await budget.reserve(endpoint="test", tokens=100, final=True)
    async with database.sessions.begin() as session:
        await session.execute(
            update(TaskRunRecord)
            .where(TaskRunRecord.id == budget._run_id)
            .values(status="succeeded")
        )
    maintenance = RunModelBudget(
        database, run_id=budget._run_id, user_id=budget._user_id, config=config, phase="maintenance"
    )
    with pytest.raises(BudgetDenied, match="run_budget_exhausted"):
        await maintenance.reserve(endpoint="test", tokens=10, final=True)


@pytest.mark.parametrize("streaming", [False, True])
async def test_provider_retries_and_fallback_share_atomic_budget(
    database: Database, streaming: bool
) -> None:
    budget = await make_budget(database, RunBudgetConfig(max_llm_attempts=2))
    primary = FakeProvider("primary", fail=True)
    fallback = FakeProvider("fallback")
    router = LLMRouter(
        endpoints={"primary": endpoint(local=True, retries=2), "fallback": endpoint(local=True)},
        routes={
            route: RoutePolicy(primary="primary", fallbacks=["fallback"])
            for route in (LLMRoute.DIALOGUE, LLMRoute.UTILITY, LLMRoute.PRIVATE)
        },
        providers={"primary": primary, "fallback": fallback},
    )
    request = CompletionRequest(
        trace_id=uuid7(),
        privacy_level="L1",
        route="dialogue",
        messages=[LLMMessage(role="user", content="secret prompt")],
    )

    async def delta(value: str) -> None:
        pass

    with budget_scope(budget), pytest.raises(BudgetDenied, match="run_budget_exhausted"):
        if streaming:
            await router.stream(request, delta)
        else:
            await router.complete(request)
    assert len(primary.requests) == 2 and fallback.requests == []
    assert current_budget() is None
    async with database.sessions() as session:
        rows = list(await session.scalars(select(ModelReservationRecord)))
        assert len(rows) == 2 and all(row.state == "unknown" for row in rows)
        assert "secret prompt" not in str([row.__dict__ for row in rows])


async def test_partial_usage_keeps_full_reservation(database: Database) -> None:
    budget = await make_budget(database, RunBudgetConfig())
    permit = await budget.reserve(endpoint="test", tokens=700, final=True)
    await budget.settle(
        permit.call_id, ModelUsage(input_tokens=80, total_tokens=80, usage_known=False)
    )
    async with database.sessions() as session:
        row = await session.get(TaskRunRecord, budget._run_id)
        record = await session.get(ModelReservationRecord, permit.call_id)
        assert row is not None and row.budget_tokens == 700
        assert record is not None and record.state == "unknown"


@pytest.mark.parametrize("streaming", [False, True])
async def test_chat_uses_preserved_final_slot_and_reports_budget(
    tmp_path: Path, streaming: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_chat import FakeToolRuntime, _loop_candidate, _summary_service, create_user

    from app.chat import ChatService
    from app.config import DatabaseConfigStore, HubConfig
    from app.llm.contracts import CompletionResult, ToolCall
    from app.schemas import PrivacyLevel

    service, database, _ = await _summary_service(tmp_path)
    assert isinstance(service._config_store, DatabaseConfigStore)
    config = _loop_candidate(service._config_store, 3)
    config["run_budget"] = {"max_llm_attempts": 2}
    draft = await service._config_store.create_draft(HubConfig.model_validate(config), actor="test")
    await service._config_store.publish(draft.version, actor="test")
    requests: list[CompletionRequest] = []

    class Provider(FakeProvider):
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            requests.append(request)
            return CompletionResult(
                text="speculative" if request.tools else "依据已有查询结果作答。",
                provider="test",
                model="test",
                endpoint="cloud",
                route=request.route,
                latency_ms=0,
                tool_calls=[
                    ToolCall(
                        id="weather",
                        function={"name": "get_weather", "arguments": {"location": "济南市"}},
                    )
                ]
                if request.tools
                else [],
                usage=ModelUsage(total_tokens=100),
            )

        async def stream(self, request: CompletionRequest, on_delta: object) -> CompletionResult:
            from collections.abc import Awaitable, Callable
            from typing import cast

            result = await self.complete(request)
            await cast(Callable[[str], Awaitable[None]], on_delta)(result.text)
            return result

    def router(config: HubConfig) -> LLMRouter:
        return LLMRouter(
            endpoints=config.models,
            routes=config.routes,
            providers={name: Provider(name) for name in config.models},
        )

    monkeypatch.setattr(
        "app.chat.service.build_query_tool_runtime", lambda config, secrets: FakeToolRuntime()
    )
    governed = ChatService(database, service._config_store, router_builder=router)
    try:
        user = await create_user(database)
        conversation = await governed.create_conversation(user_id=user.id, title="budget")
        deltas: list[str] = []
        if streaming:
            pending = await governed.start_turn(
                conversation.id,
                user_id=user.id,
                text="济南天气怎么样",
                privacy_level=PrivacyLevel.L1,
            )

            async def delta(value: str) -> None:
                deltas.append(value)

            turn = await governed.run_stream(pending, delta)
        else:
            turn = await governed.send_message(
                conversation.id,
                user_id=user.id,
                text="济南天气怎么样",
                privacy_level=PrivacyLevel.L1,
            )
        assert len(requests) == 2 and requests[0].tools and not requests[1].tools
        assert requests[1].tool_choice == "none"
        assert turn.assistant_message.content == "依据已有查询结果作答。"
        assert deltas == (["依据已有查询结果作答。"] if streaming else [])
        detail = await governed.runs.get(turn.assistant_message.turn_id, user_id=user.id)
        assert detail.budget_summary is not None
        assert detail.budget_summary.llm_attempts == 2
        assert detail.budget_summary.charged_tokens == 200
        assert detail.budget_summary.unknown_usage_calls == 0
        manifest = (turn.assistant_message.decision_meta or {})["context_budget"]
        assert isinstance(manifest, dict) and manifest["budget_final_answer"] == 1
        await governed.delete_conversation(conversation.id, user_id=user.id)
        async with database.sessions() as session:
            assert list(await session.scalars(select(ModelReservationRecord))) == []
    finally:
        await governed.drain_background_work()
        await service.drain_background_work()
        await database.close()


async def test_active_stream_cannot_extend_absolute_run_deadline(database: Database) -> None:
    from app.llm.contracts import CompletionResult

    budget = await make_budget(database, RunBudgetConfig())
    async with database.sessions.begin() as session:
        await session.execute(
            update(TaskRunRecord)
            .where(TaskRunRecord.id == budget._run_id)
            .values(deadline=datetime.now(UTC) + timedelta(seconds=0.3))
        )

    class ContinuousProvider(FakeProvider):
        async def stream(self, request: CompletionRequest, on_delta: object) -> CompletionResult:
            from collections.abc import Awaitable, Callable
            from typing import cast

            self.requests.append(request)
            while True:
                await cast(Callable[[str], Awaitable[None]], on_delta)("live")
                await asyncio.sleep(0.01)

    primary, fallback = ContinuousProvider("primary"), FakeProvider("fallback")
    router = LLMRouter(
        endpoints={"primary": endpoint(local=True), "fallback": endpoint(local=True)},
        routes={
            route: RoutePolicy(primary="primary", fallbacks=["fallback"]) for route in LLMRoute
        },
        providers={"primary": primary, "fallback": fallback},
    )
    request = CompletionRequest(
        trace_id=uuid7(),
        privacy_level="L1",
        route="dialogue",
        messages=[LLMMessage(role="user", content="prompt")],
    )
    deltas: list[str] = []

    async def delta(value: str) -> None:
        deltas.append(value)

    with budget_scope(budget), pytest.raises(BudgetDenied, match="run_deadline_exceeded"):
        await router.stream(request, delta)
    assert deltas and len(primary.requests) == 1 and fallback.requests == []
    async with database.sessions() as session:
        records = list(await session.scalars(select(ModelReservationRecord)))
        assert len(records) == 1 and records[0].state == "unknown"


async def test_budget_denied_postcommit_is_terminal_and_reply_stays_committed(
    tmp_path: Path,
) -> None:
    from test_chat import _summary_service, create_user

    from app.db import JobRecord
    from app.memory import MemoryIngester, MemoryStore
    from app.schemas import PrivacyLevel

    service, database, _ = await _summary_service(tmp_path)
    service._memory_ingester = MemoryIngester(MemoryStore(database))
    calls = 0

    async def denied(kind: str, payload: dict[str, object], owner: str) -> None:
        nonlocal calls
        calls += 1
        raise BudgetDenied("run_budget_exhausted")

    service._postcommit._handler = denied
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="maintenance")
        turn = await service.send_message(
            conversation.id, user_id=user.id, text="hello", privacy_level=PrivacyLevel.L1
        )
        await service._postcommit.process_ready()
        await service._postcommit.process_ready()
        async with database.sessions() as session:
            jobs = list(await session.scalars(select(JobRecord)))
            assert len(jobs) == 1 and jobs[0].status == "failed"
            assert jobs[0].error_code == "run_budget_exhausted" and jobs[0].attempts == 1
        assert calls == 1
        assert (
            await service.runs.get(turn.assistant_message.turn_id, user_id=user.id)
        ).status == "succeeded"
        assert len(await service.list_messages(conversation.id, user_id=user.id)) == 2
    finally:
        await service.drain_background_work()
        await database.close()


async def test_admission_failure_never_calls_provider(database: Database) -> None:
    budget = await make_budget(database, RunBudgetConfig())

    class FailedAdmission(RunModelBudget):
        async def reserve(self, *, endpoint: str, tokens: int, final: bool) -> CallPermit:
            raise RuntimeError("database unavailable")

    failed = FailedAdmission(
        database, run_id=budget._run_id, user_id=budget._user_id, config=RunBudgetConfig()
    )
    provider = FakeProvider("test")
    router = LLMRouter(
        endpoints={"test": endpoint(local=True)},
        routes={route: RoutePolicy(primary="test") for route in LLMRoute},
        providers={"test": provider},
    )
    request = CompletionRequest(
        trace_id=uuid7(),
        privacy_level="L1",
        route="dialogue",
        messages=[LLMMessage(role="user", content="prompt")],
    )
    with budget_scope(failed), pytest.raises(BudgetDenied, match="budget_admission_failed"):
        await router.complete(request)
    assert provider.requests == []


async def test_actual_usage_above_reservation_is_charged_and_stops_next_call(
    database: Database,
) -> None:
    budget = await make_budget(database, RunBudgetConfig(max_tokens=1024))
    permit = await budget.reserve(endpoint="test", tokens=700, final=True)
    await budget.settle(permit.call_id, ModelUsage(total_tokens=1200))
    with pytest.raises(BudgetDenied, match="run_budget_exhausted"):
        await budget.reserve(endpoint="test", tokens=10, final=True)
    async with database.sessions() as session:
        row = await session.get(TaskRunRecord, budget._run_id)
        assert row is not None and row.budget_tokens == 1200 and row.llm_attempts == 1


async def test_settlement_failure_never_repeats_successful_provider_call(
    database: Database,
) -> None:
    budget = await make_budget(database, RunBudgetConfig())

    class FailedSettlement(RunModelBudget):
        async def settle(self, call_id: UUID, usage: ModelUsage | None) -> None:
            raise RuntimeError("database unavailable")

    failed = FailedSettlement(
        database, run_id=budget._run_id, user_id=budget._user_id, config=RunBudgetConfig()
    )
    primary, fallback = FakeProvider("primary"), FakeProvider("fallback")
    router = LLMRouter(
        endpoints={"primary": endpoint(local=True, retries=2), "fallback": endpoint(local=True)},
        routes={
            route: RoutePolicy(primary="primary", fallbacks=["fallback"]) for route in LLMRoute
        },
        providers={"primary": primary, "fallback": fallback},
    )
    request = CompletionRequest(
        trace_id=uuid7(),
        privacy_level="L1",
        route="dialogue",
        messages=[LLMMessage(role="user", content="prompt")],
    )
    with budget_scope(failed), pytest.raises(BudgetDenied, match="budget_settlement_failed"):
        await router.complete(request)
    assert len(primary.requests) == 1 and fallback.requests == []
    async with database.sessions() as session:
        rows = list(await session.scalars(select(ModelReservationRecord)))
        assert len(rows) == 1 and rows[0].state == "reserved"


async def test_background_job_shares_parent_budget_and_cannot_revive_deleted_parent(
    database: Database,
) -> None:
    from app.db import JobRecord
    from app.jobs import JobEngine
    from app.runs.budget import job_model_budget

    config = RunBudgetConfig(max_llm_attempts=3)
    parent = await make_budget(database, config)
    async with database.sessions() as session:
        run = await session.scalar(select(TaskRunRecord))
        assert run is not None
        run_id, user_id = run.id, run.user_id
    engine = JobEngine(database)
    job = await engine.submit(
        "deleg.test",
        {"user_id": str(user_id), "turn_id": str(run_id)},
        owner=str(user_id),
        source_turn_id=run_id,
        resource_class="deleg",
    )
    await engine.claim("worker", resource_class="deleg")
    child = await job_model_budget(database, job.id, config)
    assert child is not None
    await parent.reserve(endpoint="local", tokens=10, final=True)
    await child.reserve(endpoint="local", tokens=10, final=True)
    with pytest.raises(BudgetDenied, match="run_budget_exhausted"):
        await child.reserve(endpoint="local", tokens=10, final=True)
    await parent.reserve(endpoint="local", tokens=10, final=True)
    with pytest.raises(BudgetDenied, match="run_budget_exhausted"):
        await parent.reserve(endpoint="local", tokens=10, final=True)
    async with database.sessions.begin() as session:
        await session.execute(
            update(JobRecord).where(JobRecord.id == job.id).values(task_run_id=None)
        )
    with pytest.raises(BudgetDenied, match="budget_run_not_found"):
        await job_model_budget(database, job.id, config)


async def test_standalone_job_run_persists_budget_and_terminal_state(database: Database) -> None:
    from app.jobs import JobEngine
    from app.runs.budget import job_model_budget
    from app.runs.store import RunStore

    config = RunBudgetConfig(max_llm_attempts=2)
    user_id = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=user_id, display_name="Job fixture", status="active"))
    engine = JobEngine(database)
    job = await engine.submit(
        "deleg.test", {"user_id": str(user_id)}, owner=str(user_id), resource_class="deleg"
    )
    await engine.claim("worker", resource_class="deleg")
    first = await job_model_budget(database, job.id, config)
    assert first is not None
    await first.reserve(endpoint="local", tokens=10, final=True)
    resumed = await job_model_budget(database, job.id, config)
    assert resumed is not None
    await resumed.reserve(endpoint="local", tokens=10, final=True)
    with pytest.raises(BudgetDenied, match="run_budget_exhausted"):
        await resumed.reserve(endpoint="local", tokens=10, final=True)
    assert await engine.succeed(job.id, worker_id="worker", claim_version=1)
    view = await RunStore(database).get(job.id, user_id=user_id)
    assert view.status == "succeeded" and view.budget_summary is not None
    assert view.budget_summary.llm_attempts == 2
    assert [event.kind for event in await RunStore(database).events(job.id, user_id=user_id)] == [
        "run.running",
        "run.succeeded",
    ]


async def test_background_cancel_stops_admission_and_preserves_owner_boundary(
    database: Database,
) -> None:
    from app.jobs import JobEngine
    from app.runs.budget import job_model_budget
    from app.runs.store import RunStore

    config = RunBudgetConfig()
    user_id = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=user_id, display_name="Job fixture", status="active"))
    engine = JobEngine(database)
    job = await engine.submit(
        "deleg.test", {"user_id": str(user_id)}, owner=str(user_id), resource_class="deleg"
    )
    await engine.claim("worker", resource_class="deleg")
    budget = await job_model_budget(database, job.id, config)
    assert budget is not None
    store = RunStore(database)
    with pytest.raises(LookupError):
        await store.cancel_background(job.id, user_id=uuid7())
    assert await store.cancel_background(job.id, user_id=user_id)
    with pytest.raises(BudgetDenied, match="budget_run_inactive"):
        await budget.reserve(endpoint="local", tokens=10, final=True)
    assert not await engine.succeed(job.id, worker_id="worker", claim_version=1)
    assert await engine.confirm_cancelled(job.id, "worker", claim_version=1)
    assert (await store.get(job.id, user_id=user_id)).status == "cancelled"


async def test_user_concurrency_is_shared_across_runs_and_released_after_attempt(
    database: Database,
) -> None:
    config = RunBudgetConfig(max_concurrent_llm_calls=1)
    first = await make_budget(database, config)
    run_id = uuid7()
    now = datetime.now(UTC)
    async with database.sessions.begin() as session:
        session.add(
            TaskRunRecord(
                id=run_id,
                user_id=first._user_id,
                contract={},
                budget=config.model_dump(),
                status="running",
                privacy_level="L1",
                created_at=now,
                updated_at=now,
                deadline=now + timedelta(seconds=180),
            )
        )
    second = RunModelBudget(database, run_id=run_id, user_id=first._user_id, config=config)
    results = await asyncio.gather(
        first.reserve(endpoint="local", tokens=10, final=True),
        second.reserve(endpoint="local", tokens=10, final=True),
        return_exceptions=True,
    )
    assert sum(isinstance(result, CallPermit) for result in results) == 1
    denied = next(result for result in results if isinstance(result, BudgetDenied))
    assert denied.reason_code == "user_model_concurrency_exhausted"
    independent = await make_budget(database, config)
    await independent.reserve(endpoint="local", tokens=10, final=True)
    winner = first if isinstance(results[0], CallPermit) else second
    permit = next(result for result in results if isinstance(result, CallPermit))
    # Even unknown usage releases concurrency after the provider attempt ends;
    # its conservative token charge remains accounted for.
    await winner.settle(permit.call_id, None)
    await second.reserve(endpoint="local", tokens=10, final=True)
