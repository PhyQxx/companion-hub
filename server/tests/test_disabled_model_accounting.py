"""Disabled quotas retain per-provider financial receipts and accepted Run lineage."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from sqlalchemy import delete, select
from test_chat_parent_budget import fixture as chat_fixture
from test_chat_parent_budget import start
from test_database_config import config_yaml
from test_llm import FakeProvider
from test_operation_authority_fence import prepared
from test_resource_budget import seed

from app.config import ConfigStore
from app.config.models import RunBudgetConfig
from app.db import ModelCostRecord, ModelReservationRecord, TaskRunRecord
from app.harness.budget import BudgetDenied, current_budget, current_tool_budget
from app.harness.model_accounting import model_accounting_scope
from app.harness.run_trace import DisabledRunTrace
from app.ids import uuid7
from app.llm import CompletionRequest, CompletionResult, LLMRouteExhausted, LLMRouter, ModelUsage
from app.llm.contracts import ModelPricing
from app.runs import model_accounting as accounting_module
from app.runs.completion import complete_with_run
from app.runs.costs import settle_cost
from app.runs.model_accounting import DisabledModelAccounting
from app.runs.trace_sources import capture_disabled_trace
from app.schemas import PrivacyLevel
from scripts.benchmark_storage import FixtureStorage


class PricedProvider(FakeProvider):
    async def complete(self, request: CompletionRequest) -> CompletionResult:
        assert current_budget() is None and current_tool_budget() is None
        result = await super().complete(request)
        return result.model_copy(
            update={
                "usage": ModelUsage(
                    input_tokens=10,
                    output_tokens=5,
                    total_tokens=15,
                    usage_known=True,
                    provider_request_id=f"synthetic-{uuid7()}",
                )
            }
        )

    async def stream(
        self, request: CompletionRequest, on_delta: Callable[[str], Awaitable[None]]
    ) -> CompletionResult:
        await on_delta("synthetic")
        return await self.complete(request)


async def configuration(tmp_path: Path, *, price: str = "known", retries: int = 0) -> ConfigStore:
    value = yaml.safe_load(config_yaml())
    value["run_budget"] = RunBudgetConfig(enabled=False, max_llm_attempts=2).model_dump(mode="json")
    for endpoint in value["models"].values():
        endpoint.update(
            max_retries=retries,
            input_cost_per_million=2 if price == "known" else 0,
            output_cost_per_million=5 if price == "known" else 0,
            cost_currency="CNY",
        )
        if price == "missing":
            endpoint.pop("input_cost_per_million")
            endpoint.pop("output_cost_per_million")
    path = tmp_path / "financial.yaml"
    path.write_text(yaml.safe_dump(value))
    store = ConfigStore(path)
    await store.load()
    return store


def request() -> CompletionRequest:
    return CompletionRequest(
        trace_id=uuid7(),
        messages=[{"role": "user", "content": "synthetic"}],
        privacy_level="L1",
        route="utility",
    )


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("price", ["known", "free", "missing"])
@pytest.mark.parametrize("stream", [False, True])
async def test_owned_router_off_records_fees_without_enabling_quotas(
    backend: str,
    price: str,
    stream: bool,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, _, _ = await seed(storage.database)
        store = await configuration(tmp_path, price=price)
        provider = PricedProvider("local")
        router = LLMRouter(
            endpoints=store.current.config.models,
            routes=store.current.config.routes,
            providers={name: provider for name in store.current.config.models},
        )

        async def invoke(req: CompletionRequest) -> CompletionResult:
            if stream:

                async def delta(value: str) -> None:
                    pass

                return await router.stream(req, delta)
            return await router.complete(req)

        await complete_with_run(
            storage.database,
            store.current,
            request(),
            invoke,
            user_id=owner,
            kind="synthetic.accounting",
            source_id=uuid7(),
        )
        async with storage.database.sessions() as sql:
            fees = list(await sql.scalars(select(ModelCostRecord)))
            attempts = list(await sql.scalars(select(ModelReservationRecord)))
            run = await sql.get_one(TaskRunRecord, attempts[0].run_id)
        assert len(fees) == len(attempts) == len(provider.requests) == 1
        assert fees[0].call_id == attempts[0].call_id and fees[0].user_id == owner
        assert fees[0].charged_micros == (
            45 if price == "known" else 0 if price == "free" else None
        )
        assert fees[0].state == ("unknown" if price == "missing" else "estimated")
        assert fees[0].provider_request_id and run.status == "succeeded"
        assert run.llm_attempts == run.budget_tokens == 0
        assert attempts[0].actual_tokens == 15 and attempts[0].state == "settled"
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_off_retries_each_record_unknown_cost_and_do_not_spend_quota(
    backend: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, _, _ = await seed(storage.database)
        store = await configuration(tmp_path, retries=2)
        provider = PricedProvider("local", fail=True)
        router = LLMRouter(
            endpoints=store.current.config.models,
            routes=store.current.config.routes,
            providers={name: provider for name in store.current.config.models},
        )
        with pytest.raises(LLMRouteExhausted):
            await complete_with_run(
                storage.database,
                store.current,
                request(),
                router.complete,
                user_id=owner,
                kind="synthetic.failure",
                source_id=uuid7(),
            )
        async with storage.database.sessions() as sql:
            fees = list(await sql.scalars(select(ModelCostRecord)))
            attempts = list(await sql.scalars(select(ModelReservationRecord)))
            run = await sql.get_one(TaskRunRecord, attempts[0].run_id)
        assert len(fees) == len(provider.requests) == len(attempts) and len(fees) >= 3
        assert all(
            fee.state == "unknown" and fee.charged_micros is not None and fee.charged_micros > 0
            for fee in fees
        )
        assert all(row.state == "unknown" for row in attempts)
        assert run.status == "failed" and run.llm_attempts == run.budget_tokens == 0
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_chat_parent_off_is_accounted_at_actual_child_without_borrowing_current_policy(
    backend: str,
    tmp_path: Path,
) -> None:
    provider = PricedProvider("cloud")
    storage, service, owner, root, conversation, _ = await chat_fixture(
        backend,
        tmp_path,
        enabled=False,
        provider=provider,
    )
    try:

        async def discard_delta(value: str) -> None:
            pass

        pending = await start(service, owner, root, conversation)
        await service.run_stream(pending, discard_delta)
        async with storage.database.sessions() as sql:
            fees = list(await sql.scalars(select(ModelCostRecord)))
            attempts = list(await sql.scalars(select(ModelReservationRecord)))
            original = await sql.get_one(TaskRunRecord, root)
            child = await sql.get_one(TaskRunRecord, pending.turn_id)
        assert len(fees) == len(attempts) == 1
        assert attempts[0].run_id == child.id and child.parent_run_id == root
        assert original.llm_attempts == child.llm_attempts == 0
        assert child.status == "succeeded" and fees[0].state == "estimated"
        assert fees[0].charged_micros == 0
    finally:
        await service.drain_background_work()
        await storage.close()


async def off_source(storage: FixtureStorage, owner: UUID, root: UUID) -> DisabledRunTrace:
    async with storage.database.sessions.begin() as sql:
        row = await sql.get_one(TaskRunRecord, root)
        row.budget = RunBudgetConfig(enabled=False).model_dump(mode="json")
        row.deadline = None
    trace = await capture_disabled_trace(storage.database, root, owner, require_disabled=True)
    assert trace is not None
    return trace


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", ["cancel", "delete", "price_snapshot", "expiry"])
async def test_late_receipt_keeps_fee_but_rejects_revoked_source(
    backend: str, change: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, root, _ = await seed(storage.database)
        trace = await off_source(storage, owner, root)
        accounting = DisabledModelAccounting(
            storage.database, run_id=root, user_id=owner, trace=trace
        )
        permit = await accounting.reserve(
            endpoint="synthetic",
            tokens=100,
            pricing=ModelPricing(input_rate=2, output_rate=5, currency="CNY"),
            privacy_level=PrivacyLevel.L1,
        )
        if change == "expiry":
            accounting._trace = replace(trace, expires_at=datetime.now(UTC) - timedelta(seconds=1))
        else:
            async with storage.database.sessions.begin() as sql:
                if change == "delete":
                    await sql.execute(delete(TaskRunRecord).where(TaskRunRecord.id == root))
                else:
                    row = await sql.get_one(TaskRunRecord, root)
                    if change == "cancel":
                        row.status = "cancelled"
                    else:
                        row.config_version = 99
        with pytest.raises(BudgetDenied):
            await accounting.settle(
                permit.call_id,
                ModelUsage(
                    input_tokens=10,
                    output_tokens=5,
                    total_tokens=15,
                    usage_known=True,
                    provider_request_id="synthetic-late",
                ),
            )
        async with storage.database.sessions() as sql:
            fee = await sql.get_one(ModelCostRecord, permit.call_id)
        assert fee.state == "estimated" and fee.charged_micros == 45
        assert fee.provider_request_id == "synthetic-late"
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_cancel_during_fee_commit_joins_even_after_repeated_cancel(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = await prepared(backend, tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    pending = None
    try:
        owner, root, _ = await seed(storage.database)
        trace = await off_source(storage, owner, root)
        store = await configuration(tmp_path)
        provider = PricedProvider("local")
        router = LLMRouter(
            endpoints=store.current.config.models,
            routes=store.current.config.routes,
            providers={name: provider for name in store.current.config.models},
        )
        original = settle_cost

        async def settle(*args: Any, **kwargs: Any) -> bool:
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(accounting_module, "settle_cost", settle)
        accounting = DisabledModelAccounting(
            storage.database, run_id=root, user_id=owner, trace=trace
        )

        async def invoke() -> None:
            with model_accounting_scope(accounting):
                await router.complete(request())

        pending = asyncio.create_task(invoke())
        await asyncio.wait_for(entered.wait(), 5)
        pending.cancel()
        await asyncio.sleep(0.01)
        pending.cancel()
        await asyncio.sleep(0.01)
        assert not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        async with storage.database.sessions() as sql:
            fees = list(await sql.scalars(select(ModelCostRecord)))
        assert len(fees) == 1 and fees[0].state == "estimated" and fees[0].charged_micros == 45
        assert len(provider.requests) == 1
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_parallel_off_calls_have_distinct_fees_with_zero_quota_counters(
    backend: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, root, _ = await seed(storage.database)
        trace = await off_source(storage, owner, root)
        accounting = DisabledModelAccounting(
            storage.database, run_id=root, user_id=owner, trace=trace
        )
        pricing = ModelPricing(input_rate=2, output_rate=5, currency="CNY")
        permits = await asyncio.gather(
            *[
                accounting.reserve(
                    endpoint="synthetic", tokens=100, pricing=pricing, privacy_level=PrivacyLevel.L1
                )
                for _ in range(8)
            ]
        )
        assert len({permit.call_id for permit in permits}) == 8
        usage = ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15, usage_known=True)
        await asyncio.gather(*[accounting.settle(permit.call_id, usage) for permit in permits])
        async with storage.database.sessions() as sql:
            row = await sql.get_one(TaskRunRecord, root)
            fees = list(await sql.scalars(select(ModelCostRecord)))
        assert row.llm_attempts == row.budget_tokens == 0
        assert len(fees) == 8 and sum(fee.charged_micros or 0 for fee in fees) == 360
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("invalid", ["enabled_root", "foreign_owner", "privacy", "cancelled"])
async def test_accounting_rejects_invalid_source_before_fee_reservation(
    backend: str,
    invalid: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, root, _ = await seed(storage.database)
        trace = await off_source(storage, owner, root)
        privacy = PrivacyLevel.L1
        if invalid == "foreign_owner":
            owner = uuid7()
        else:
            async with storage.database.sessions.begin() as sql:
                row = await sql.get_one(TaskRunRecord, root)
                if invalid == "enabled_root":
                    row.budget = RunBudgetConfig().model_dump(mode="json")
                    from app.runs.trace_sources import trace_source

                    trace = replace(trace, sources=(trace_source(row, maintenance=False),))
                elif invalid == "privacy":
                    privacy = PrivacyLevel.L1
                    row.privacy_level = "L2"
                    from app.runs.trace_sources import trace_source

                    trace = replace(trace, sources=(trace_source(row, maintenance=False),))
                else:
                    row.status = "cancelled"
        accounting = DisabledModelAccounting(
            storage.database, run_id=root, user_id=owner, trace=trace
        )
        with pytest.raises(BudgetDenied):
            await accounting.reserve(
                endpoint="synthetic", tokens=100, pricing=ModelPricing(), privacy_level=privacy
            )
        async with storage.database.sessions() as sql:
            assert not list(await sql.scalars(select(ModelCostRecord)))
            assert not list(await sql.scalars(select(ModelReservationRecord)))
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_postcommit_revocation_before_permit_transfer_records_zero_cost(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, root, _ = await seed(storage.database)
        trace = await off_source(storage, owner, root)
        accounting = DisabledModelAccounting(
            storage.database, run_id=root, user_id=owner, trace=trace
        )

        async def reject(*args: object) -> None:
            raise BudgetDenied("synthetic_revoked")

        monkeypatch.setattr(accounting_module, "validate_disabled_trace", reject)
        with pytest.raises(BudgetDenied, match="synthetic_revoked"):
            await accounting.reserve(
                endpoint="synthetic",
                tokens=100,
                pricing=ModelPricing(),
                privacy_level=PrivacyLevel.L1,
            )
        async with storage.database.sessions() as sql:
            fee = (await sql.scalars(select(ModelCostRecord))).one()
            receipt = (await sql.scalars(select(ModelReservationRecord))).one()
        assert fee.state == "estimated" and fee.charged_micros == 0
        assert receipt.state == "settled" and receipt.actual_tokens == 0
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("unknown", [False, True])
async def test_new_enabled_limit_sees_spending_recorded_while_disabled(
    backend: str,
    unknown: bool,
    tmp_path: Path,
) -> None:
    from app.runs.budget import RunModelBudget

    storage = await prepared(backend, tmp_path)
    try:
        owner, root, _ = await seed(storage.database)
        trace = await off_source(storage, owner, root)
        accounting = DisabledModelAccounting(
            storage.database, run_id=root, user_id=owner, trace=trace
        )
        permit = await accounting.reserve(
            endpoint="synthetic",
            tokens=100,
            pricing=ModelPricing()
            if unknown
            else ModelPricing(input_rate=2, output_rate=5, currency="CNY"),
            privacy_level=PrivacyLevel.L1,
        )
        await accounting.settle(
            permit.call_id,
            ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15, usage_known=True),
        )
        owner2, enabled, _ = await seed(storage.database)
        policy = RunBudgetConfig(cost_currency="CNY", max_daily_cost=0)
        async with storage.database.sessions.begin() as sql:
            row = await sql.get_one(TaskRunRecord, enabled)
            row.user_id = owner
            row.budget = policy.model_dump(mode="json")
        budget = RunModelBudget(storage.database, run_id=enabled, user_id=owner, config=policy)
        with pytest.raises(
            BudgetDenied, match="cost_usage_unknown" if unknown else "daily_cost_budget_exhausted"
        ):
            await budget.reserve(
                endpoint="synthetic",
                tokens=1,
                final=True,
                pricing=ModelPricing(input_rate=0, output_rate=0, currency="CNY"),
            )
        assert owner2 != owner
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_estimated_receipts_are_idempotent_and_cannot_settle_another_call(
    backend: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, root, _ = await seed(storage.database)
        trace = await off_source(storage, owner, root)
        accounting = DisabledModelAccounting(
            storage.database, run_id=root, user_id=owner, trace=trace
        )
        pricing = ModelPricing(input_rate=2, output_rate=5, currency="CNY")
        permit = await accounting.reserve(
            endpoint="synthetic", tokens=100, pricing=pricing, privacy_level=PrivacyLevel.L1
        )
        usage = ModelUsage(
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            usage_known=True,
            provider_request_id="synthetic-exact",
        )
        await accounting.settle(permit.call_id, usage)
        await accounting.settle(permit.call_id, usage)
        await accounting.settle(permit.call_id, None)
        with pytest.raises(BudgetDenied, match="cost_settlement_conflict"):
            await accounting.settle(
                permit.call_id, usage.model_copy(update={"output_tokens": 6, "total_tokens": 16})
            )
        stranger = DisabledModelAccounting(
            storage.database, run_id=root, user_id=owner, trace=trace
        )
        with pytest.raises(BudgetDenied, match="cost_call_not_owned"):
            await stranger.settle(permit.call_id, usage)
        async with storage.database.sessions() as sql:
            fee = await sql.get_one(ModelCostRecord, permit.call_id)
            receipt = await sql.get_one(ModelReservationRecord, permit.call_id)
        assert fee.charged_micros == 45 and receipt.actual_tokens == 15
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_chat_postprocessing_keeps_disabled_financial_lineage(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = PricedProvider("cloud")
    storage, service, owner, root, conversation, _ = await chat_fixture(
        backend, tmp_path, enabled=False, provider=provider
    )
    try:

        async def delta(value: str) -> None:
            pass

        pending = await start(service, owner, root, conversation)
        turn = await service.run_stream(pending, delta)
        async with storage.database.sessions.begin() as sql:
            parent = await sql.get_one(TaskRunRecord, root)
            parent.status = "succeeded"

        async def extract(*args: object, backend: Any, **kwargs: object) -> None:
            await backend.complete(request())

        monkeypatch.setattr(service, "_extract_commitments", extract)
        await service._run_postcommit(
            "chat.commitments", {"assistant_message_id": str(turn.assistant_message.id)}, str(owner)
        )
        async with storage.database.sessions() as sql:
            fees = list(await sql.scalars(select(ModelCostRecord)))
            receipts = list(await sql.scalars(select(ModelReservationRecord)))
            row = await sql.get_one(TaskRunRecord, root)
        assert len(fees) == len(receipts) == len(provider.requests) == 2
        assert {r.phase for r in receipts} == {"interactive", "maintenance"}
        assert all(r.run_id == pending.turn_id for r in receipts) and row.llm_attempts == 0
    finally:
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("late", [False, True])
async def test_worker_claim_precedes_owner_and_revocation_keeps_actual_fee(
    backend: str,
    late: bool,
    tmp_path: Path,
) -> None:
    from sqlalchemy import Connection, event, update
    from test_claim_transaction import claim

    from app.db import JobRecord
    from app.harness.claim import claim_scope

    storage = await prepared(backend, tmp_path)
    statements: list[str] = []

    def observe(
        conn: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.startswith("UPDATE"):
            statements.append(statement)

    try:
        owner, root, _ = await seed(storage.database)
        trace = await off_source(storage, owner, root)
        _, identity = await claim(storage.database, owner)
        accounting = DisabledModelAccounting(
            storage.database, run_id=root, user_id=owner, trace=trace
        )
        event.listen(storage.database.engine.sync_engine, "before_cursor_execute", observe)
        if not late:
            async with storage.database.sessions.begin() as sql:
                await sql.execute(
                    update(JobRecord)
                    .where(JobRecord.id == identity.job_id)
                    .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
            statements.clear()
        with claim_scope(identity):
            if not late:
                with pytest.raises(BudgetDenied, match="job_claim_lost"):
                    await accounting.reserve(
                        endpoint="synthetic",
                        tokens=100,
                        pricing=ModelPricing(),
                        privacy_level=PrivacyLevel.L1,
                    )
            else:
                permit = await accounting.reserve(
                    endpoint="synthetic",
                    tokens=100,
                    pricing=ModelPricing(input_rate=2, output_rate=5, currency="CNY"),
                    privacy_level=PrivacyLevel.L1,
                )
                assert statements[0].split()[1].rsplit(".", 1)[-1] == "job"
                assert any(s.split()[1].rsplit(".", 1)[-1] == "app_user" for s in statements)
                async with storage.database.sessions.begin() as sql:
                    await sql.execute(
                        update(JobRecord)
                        .where(JobRecord.id == identity.job_id)
                        .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
                    )
                with pytest.raises(BudgetDenied, match="job_claim_lost"):
                    await accounting.settle(
                        permit.call_id,
                        ModelUsage(
                            input_tokens=10, output_tokens=5, total_tokens=15, usage_known=True
                        ),
                    )
        async with storage.database.sessions() as sql:
            fees = list(await sql.scalars(select(ModelCostRecord)))
        assert len(fees) == (1 if late else 0)
        if late:
            assert fees[0].state == "estimated" and fees[0].charged_micros == 45
    finally:
        event.remove(storage.database.engine.sync_engine, "before_cursor_execute", observe)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_usage_overflow_keeps_unknown_fee_and_closes_future_accounting(
    backend: str,
    tmp_path: Path,
) -> None:
    from app.harness.budget import MAX_MODEL_TOKENS

    storage = await prepared(backend, tmp_path)
    try:
        owner, root, _ = await seed(storage.database)
        trace = await off_source(storage, owner, root)
        accounting = DisabledModelAccounting(
            storage.database, run_id=root, user_id=owner, trace=trace
        )
        pricing = ModelPricing(input_rate=2, output_rate=5, currency="CNY")
        permit = await accounting.reserve(
            endpoint="synthetic", tokens=100, pricing=pricing, privacy_level=PrivacyLevel.L1
        )
        with pytest.raises(BudgetDenied, match="budget_usage_overflow"):
            await accounting.settle(
                permit.call_id,
                ModelUsage(
                    input_tokens=MAX_MODEL_TOKENS + 1,
                    output_tokens=0,
                    total_tokens=MAX_MODEL_TOKENS + 1,
                    usage_known=True,
                ),
            )
        with pytest.raises(BudgetDenied, match="budget_usage_overflow"):
            await accounting.reserve(
                endpoint="synthetic", tokens=100, pricing=pricing, privacy_level=PrivacyLevel.L1
            )
        async with storage.database.sessions() as sql:
            fee = await sql.get_one(ModelCostRecord, permit.call_id)
            row = await sql.get_one(TaskRunRecord, root)
        assert fee.state == "unknown" and fee.charged_micros is None
        assert row.contract["budget_usage_overflow"] and row.budget_tokens == row.llm_attempts == 0
    finally:
        await storage.close()
