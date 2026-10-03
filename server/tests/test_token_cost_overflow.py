"""Oversized provider counters cannot roll back fees or reopen model admission."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, update
from test_llm import FakeProvider, endpoint, request
from test_model_cost import priced_result
from test_run_budget import make_budget
from test_run_cancel_fence import prepared

from app.config.models import RunBudgetConfig
from app.db import ModelCostRecord, ModelReservationRecord, TaskRunRecord
from app.harness.budget import BudgetDenied, budget_scope
from app.llm import LLMRouter
from app.llm.contracts import (
    CompletionRequest,
    CompletionResult,
    LLMRoute,
    ModelPricing,
    ModelUsage,
    RoutePolicy,
)
from app.runs.costs import charge, cost_summary, micros, rate, settle_cost

MAXIMUM = (1 << 63) - 1


@pytest.mark.parametrize("precision", [6, 28])
def test_token_quote_rounds_exactly_independently_of_decimal_context(precision: int) -> None:
    # This product's sub-micro tail is beyond the caller's precision.
    with localcontext() as context:
        context.prec = precision
        assert charge(9_000_000_000_000_001, Decimal("1.000000000001"), Decimal(0)) == (
            9_000_000_000_009_002
        )
        assert rate(999999999.000001) == Decimal("999999999.000001000000")
        assert micros(999999999.000001) == 999_999_999_000_001


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("known", [False, True])
async def test_fee_overflow_commits_unknown_and_blocks_further_model_calls(
    backend: str, known: bool, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        permit = await budget.reserve(
            endpoint="synthetic",
            tokens=100,
            final=True,
            pricing=ModelPricing(
                input_rate=1_000_000_000, output_rate=1_000_000_000, currency="CNY"
            ),
        )
        with pytest.raises(BudgetDenied, match="budget_usage_overflow"):
            await budget.settle(
                permit.call_id,
                ModelUsage(
                    input_tokens=10_000_000_000,
                    total_tokens=10_000_000_000,
                    usage_known=known,
                    provider_request_id="synthetic-large-receipt",
                ),
            )
        async with storage.database.sessions() as session:
            fee = await session.get_one(ModelCostRecord, permit.call_id)
            reservation = await session.get_one(ModelReservationRecord, permit.call_id)
            root = await session.get_one(TaskRunRecord, budget.run_id)
            assert fee.state == "unknown" and fee.charged_micros is None
            assert fee.provider_request_id == "synthetic-large-receipt"
            assert reservation.state == "unknown" and reservation.actual_tokens is None
            assert root.budget_tokens == 100 and root.contract["budget_usage_overflow"] is True
        with pytest.raises(BudgetDenied, match="budget_usage_overflow"):
            await budget.reserve(endpoint="synthetic", tokens=1, final=True)
        summary = await cost_summary(storage.database, user_id=budget.owner_id)
        assert summary.currencies[0].unknown_calls == summary.currencies[0].unpriced_calls == 1
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("known", [False, True])
async def test_token_counter_overflow_keeps_reservation_and_commits_receipt(
    backend: str, known: bool, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        permit = await budget.reserve(
            endpoint="synthetic",
            tokens=100,
            final=True,
            pricing=ModelPricing(input_rate=0, output_rate=0, currency="CNY"),
        )
        with pytest.raises(BudgetDenied, match="budget_usage_overflow"):
            await budget.settle(
                permit.call_id,
                ModelUsage(
                    input_tokens=1 << 100,
                    total_tokens=1 << 100,
                    usage_known=known,
                    provider_request_id="synthetic-huge-counter",
                ),
            )
        async with storage.database.sessions() as session:
            fee = await session.get_one(ModelCostRecord, permit.call_id)
            reservation = await session.get_one(ModelReservationRecord, permit.call_id)
            root = await session.get_one(TaskRunRecord, budget.run_id)
            assert fee.charged_micros == 0
            assert fee.provider_request_id == "synthetic-huge-counter"
            assert reservation.state == "unknown" and reservation.actual_tokens is None
            assert reservation.charged_tokens == root.budget_tokens == 100
        with pytest.raises(BudgetDenied, match="budget_usage_overflow"):
            await budget.reserve(endpoint="synthetic", tokens=1, final=True)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_combined_token_overflow_does_not_write_float_or_wrap_root(
    backend: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        first = await budget.reserve(endpoint="synthetic", tokens=100, final=True)
        second = await budget.reserve(endpoint="synthetic", tokens=100, final=True)
        await budget.settle(first.call_id, ModelUsage(total_tokens=MAXIMUM - 100, usage_known=True))
        with pytest.raises(BudgetDenied, match="budget_usage_overflow"):
            await budget.settle(second.call_id, ModelUsage(total_tokens=101, usage_known=True))
        async with storage.database.sessions() as session:
            root = await session.get_one(TaskRunRecord, budget.run_id)
            assert root.budget_tokens == MAXIMUM and type(root.budget_tokens) is int
            assert root.contract["budget_usage_overflow"] is True
            reservation = await session.get_one(ModelReservationRecord, second.call_id)
            assert reservation.state == "settled" and reservation.actual_tokens == 101
        with pytest.raises(BudgetDenied, match="budget_usage_overflow"):
            await budget.reserve(endpoint="synthetic", tokens=1, final=True)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_late_unknown_cannot_erase_overflow_but_known_receipt_can_correct_fee(
    backend: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        permit = await budget.reserve(
            endpoint="synthetic",
            tokens=100,
            final=True,
            pricing=ModelPricing(input_rate=1, output_rate=1, currency="CNY"),
        )
        async with storage.database.sessions.begin() as session:
            await session.execute(delete(TaskRunRecord).where(TaskRunRecord.id == budget.run_id))
        with pytest.raises(BudgetDenied, match="budget_usage_overflow"):
            await budget.settle(
                permit.call_id,
                ModelUsage(
                    input_tokens=MAXIMUM + 1,
                    total_tokens=MAXIMUM + 1,
                    usage_known=True,
                    provider_request_id="synthetic-late",
                ),
            )
        await budget.settle(
            permit.call_id, ModelUsage(input_tokens=5, total_tokens=5, usage_known=False)
        )
        async with storage.database.sessions() as session:
            fee = await session.get_one(ModelCostRecord, permit.call_id)
            assert fee.state == "unknown" and fee.charged_micros is None
            assert fee.provider_request_id == "synthetic-late"
        await budget.settle(
            permit.call_id,
            ModelUsage(
                input_tokens=5,
                total_tokens=5,
                usage_known=True,
                provider_request_id="synthetic-late",
            ),
        )
        async with storage.database.sessions() as session:
            fee = await session.get_one(ModelCostRecord, permit.call_id)
            assert fee.state == "estimated" and fee.charged_micros == 5
            assert await session.get(TaskRunRecord, budget.run_id) is None
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_confirmed_settlement_ignores_later_unknown_huge_observation(
    backend: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        permit = await budget.reserve(
            endpoint="synthetic",
            tokens=100,
            final=True,
            pricing=ModelPricing(input_rate=1, output_rate=1, currency="CNY"),
        )
        await budget.settle(
            permit.call_id, ModelUsage(input_tokens=5, total_tokens=5, usage_known=True)
        )
        await budget.settle(permit.call_id, ModelUsage(total_tokens=1 << 100, usage_known=False))
        async with storage.database.sessions() as session:
            root = await session.get_one(TaskRunRecord, budget.run_id)
            fee = await session.get_one(ModelCostRecord, permit.call_id)
            assert root.budget_tokens == fee.charged_micros == 5
            assert not root.contract.get("budget_usage_overflow")
        await budget.reserve(endpoint="synthetic", tokens=1, final=True)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_exact_signed_integer_boundary_settles_without_rounded_overflow(
    backend: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        permit = await budget.reserve(
            endpoint="synthetic",
            tokens=100,
            final=True,
            pricing=ModelPricing(input_rate=1, output_rate=1, currency="CNY"),
        )
        await budget.settle(
            permit.call_id, ModelUsage(input_tokens=MAXIMUM, total_tokens=MAXIMUM, usage_known=True)
        )
        async with storage.database.sessions() as session:
            fee = await session.get_one(ModelCostRecord, permit.call_id)
            root = await session.get_one(TaskRunRecord, budget.run_id)
            assert fee.state == "estimated" and fee.charged_micros == root.budget_tokens == MAXIMUM
            assert not root.contract.get("budget_usage_overflow")
    finally:
        await storage.close()


@pytest.mark.parametrize("power", [308, 400])
def test_display_cost_overflow_preserves_provider_usage_without_float_error(power: int) -> None:
    result = priced_result(
        rates=(1_000_000_000, 1_000_000_000),
        currency="CNY",
        usage=SimpleNamespace(prompt_tokens=10**power, completion_tokens=0, total_tokens=10**power),
    )
    assert result.usage.estimated_cost is None
    assert result.usage.input_tokens == result.usage.total_tokens == 10**power
    assert result.usage.usage_known is True


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_direct_fee_overflow_and_known_conflict_serialize_on_owned_ledger(
    backend: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        permit = await budget.reserve(
            endpoint="synthetic",
            tokens=100,
            final=True,
            pricing=ModelPricing(input_rate=1, output_rate=1, currency="CNY"),
        )

        async def settle(usage: ModelUsage) -> None:
            async with storage.database.sessions.begin() as session:
                await settle_cost(
                    session,
                    call_id=permit.call_id,
                    user_id=budget.owner_id,
                    usage=usage,
                    now=datetime.now(UTC),
                )

        await settle(
            ModelUsage(input_tokens=MAXIMUM + 1, total_tokens=MAXIMUM + 1, usage_known=True)
        )
        outcomes = await asyncio.gather(
            *(
                settle(
                    ModelUsage(
                        input_tokens=n,
                        total_tokens=n,
                        usage_known=True,
                        provider_request_id=f"synthetic-{n}",
                    )
                )
                for n in (5, 6)
            ),
            return_exceptions=True,
        )
        assert sum(value is None for value in outcomes) == 1
        assert sum(isinstance(value, BudgetDenied) for value in outcomes) == 1
        async with storage.database.sessions() as session:
            fee = await session.get_one(ModelCostRecord, permit.call_id)
            assert fee.state == "estimated" and fee.charged_micros in (5, 6)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_known_live_correction_retains_overflow_fence(backend: str, tmp_path: Path) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        permit = await budget.reserve(
            endpoint="synthetic",
            tokens=100,
            final=True,
            pricing=ModelPricing(input_rate=1, output_rate=1, currency="CNY"),
        )
        with pytest.raises(BudgetDenied, match="budget_usage_overflow"):
            await budget.settle(
                permit.call_id,
                ModelUsage(
                    input_tokens=MAXIMUM + 1,
                    total_tokens=MAXIMUM + 1,
                    usage_known=True,
                    provider_request_id="synthetic-receipt",
                ),
            )
        await budget.settle(
            permit.call_id,
            ModelUsage(
                input_tokens=5,
                total_tokens=5,
                usage_known=True,
                provider_request_id="synthetic-receipt",
            ),
        )
        async with storage.database.sessions() as session:
            root = await session.get_one(TaskRunRecord, budget.run_id)
            fee = await session.get_one(ModelCostRecord, permit.call_id)
            assert root.budget_tokens == fee.charged_micros == 5
            assert root.contract["budget_usage_overflow"] is True
        with pytest.raises(BudgetDenied, match="budget_usage_overflow"):
            await budget.reserve(endpoint="synthetic", tokens=1, final=True)
        # Other Runs retain their own counters, but monetary uncertainty cannot
        # be ignored if a later unknown observation was never corrected.
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(ModelCostRecord)
                .where(ModelCostRecord.call_id == permit.call_id)
                .values(charged_micros=None, state="unknown")
            )
        other = await make_budget(
            storage.database, RunBudgetConfig(cost_currency="CNY", max_daily_cost=1)
        )
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == other.run_id)
                .values(user_id=budget.owner_id)
            )
        from app.runs.budget import RunModelBudget

        owned = RunModelBudget(
            storage.database,
            run_id=other.run_id,
            user_id=budget.owner_id,
            config=other.budget_config,
        )
        with pytest.raises(BudgetDenied, match="cost_usage_unknown"):
            await owned.reserve(
                endpoint="synthetic",
                tokens=1,
                final=True,
                pricing=ModelPricing(input_rate=1, output_rate=1, currency="CNY"),
            )
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_unpriced_known_replay_with_oversized_counter_is_a_conflict(
    backend: str, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        permit = await budget.reserve(endpoint="synthetic", tokens=100, final=True)
        await budget.settle(permit.call_id, ModelUsage(total_tokens=5, usage_known=True))
        with pytest.raises(BudgetDenied, match="budget_settlement_conflict"):
            await budget.settle(
                permit.call_id, ModelUsage(total_tokens=MAXIMUM + 1, usage_known=True)
            )
        async with storage.database.sessions() as session:
            root = await session.get_one(TaskRunRecord, budget.run_id)
            assert root.budget_tokens == 5 and not root.contract.get("budget_usage_overflow")
    finally:
        await storage.close()


class OverflowProvider(FakeProvider):
    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        return priced_result(
            rates=(1_000_000_000, 1_000_000_000),
            currency="CNY",
            usage=SimpleNamespace(
                prompt_tokens=10_000_000_000, completion_tokens=0, total_tokens=10_000_000_000
            ),
        ).model_copy(update={"route": request.route, "endpoint": self.name})

    async def stream(
        self, request: CompletionRequest, on_delta: Callable[[str], Awaitable[None]]
    ) -> CompletionResult:
        await on_delta("synthetic fragment")
        return await self.complete(request)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_real_router_does_not_retry_or_fallback_after_committed_usage_overflow(
    backend: str, streaming: bool, tmp_path: Path
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        primary, backup = OverflowProvider("primary"), FakeProvider("backup")
        model = endpoint(local=True, retries=2).model_copy(
            update={
                "input_cost_per_million": 1_000_000_000,
                "output_cost_per_million": 1_000_000_000,
                "cost_currency": "CNY",
            }
        )
        router = LLMRouter(
            endpoints={"primary": model, "backup": endpoint(local=True)},
            routes={
                route: RoutePolicy(primary="primary", fallbacks=["backup"]) for route in LLMRoute
            },
            providers={"primary": primary, "backup": backup},
        )
        fragments: list[str] = []

        async def delta(value: str) -> None:
            fragments.append(value)

        with budget_scope(budget), pytest.raises(BudgetDenied, match="budget_usage_overflow"):
            if streaming:
                await router.stream(request("L1"), delta)
            else:
                await router.complete(request("L1"))
        assert len(primary.requests) == 1 and not backup.requests
        assert fragments == (["synthetic fragment"] if streaming else [])
        async with storage.database.sessions() as session:
            root = await session.get_one(TaskRunRecord, budget.run_id)
            assert root.llm_attempts == 1 and root.contract["budget_usage_overflow"] is True
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("kind", ["tool", "media"])
async def test_overflow_fence_blocks_other_resources_on_same_parent(
    backend: str, kind: str, tmp_path: Path
) -> None:
    from app.harness.operations import OperationPolicy
    from app.runs.operation import operate_with_run
    from app.schemas import PrivacyLevel

    storage = await prepared(backend, tmp_path)
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        permit = await budget.reserve(
            endpoint="synthetic",
            tokens=100,
            final=True,
            pricing=ModelPricing(input_rate=0, output_rate=0, currency="CNY"),
        )
        with pytest.raises(BudgetDenied, match="budget_usage_overflow"):
            await budget.settle(
                permit.call_id, ModelUsage(total_tokens=MAXIMUM + 1, usage_known=True)
            )
        dispatched = False

        async def guard() -> None:
            return None

        async def invoke(started: Callable[[], Awaitable[None]]) -> str:
            nonlocal dispatched
            await started()
            dispatched = True
            return "synthetic result"

        with budget_scope(budget), pytest.raises(BudgetDenied, match="budget_usage_overflow"):
            if kind == "tool":
                await budget.tool_budget.reserve_tool(
                    tool_name="synthetic", user_id=budget.owner_id
                )
            else:
                await operate_with_run(
                    storage.database,
                    OperationPolicy(1, tuple(budget.budget_config.model_dump().items())),
                    user_id=budget.owner_id,
                    privacy_level=PrivacyLevel.L1,
                    entry="synthetic.media",
                    invoke=invoke,
                    evidence=lambda value: {},
                    source_guard=guard,
                    cost_endpoint="synthetic",
                )
        assert not dispatched
    finally:
        await storage.close()
