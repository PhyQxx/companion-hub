import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select, update
from test_run_budget import make_budget

from app.config.models import RunBudgetConfig
from app.db import Base, ModelCostRecord, ModelReservationRecord, TaskRunRecord, create_database
from app.harness.budget import BudgetDenied, CallPermit
from app.ids import uuid7
from app.llm.contracts import ModelPricing, ModelUsage
from app.runs.budget import RunModelBudget, recover_stale_reservations
from app.runs.costs import cost_summary

PRICING = ModelPricing(input_rate=1, output_rate=2, currency="CNY")


async def setup_budget(tmp_path: Path, **limits: object) -> RunModelBudget:
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'cost.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return await make_budget(database, RunBudgetConfig.model_validate(limits))


async def row(budget: RunModelBudget, call_id: UUID) -> ModelCostRecord:
    async with budget._database.sessions() as session:
        value = await session.get(ModelCostRecord, call_id)
        assert value is not None
        return value


async def test_cross_run_concurrency_cannot_overspend_owner_daily_limit(tmp_path: Path) -> None:
    config = RunBudgetConfig(cost_currency="CNY", max_daily_cost=0.002)
    first = await setup_budget(tmp_path, **config.model_dump())
    second = await make_budget(first._database, config)
    async with first._database.sessions.begin() as session:
        await session.execute(
            update(TaskRunRecord)
            .where(TaskRunRecord.id == second._run_id)
            .values(user_id=first._user_id)
        )
    second = RunModelBudget(
        first._database, run_id=second._run_id, user_id=first._user_id, config=config
    )
    results = await asyncio.gather(
        *(
            (first if i % 2 else second).reserve(
                endpoint="fixture", tokens=700, final=True, pricing=PRICING
            )
            for i in range(12)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, CallPermit) for result in results) == 1
    assert all(isinstance(result, (CallPermit, BudgetDenied)) for result in results)
    summary = await cost_summary(first._database, user_id=first._user_id)
    assert summary.currencies[0].charged_micros == "1400"
    assert summary.currencies[0].reserved_calls == 1
    assert not (await cost_summary(first._database, user_id=uuid7())).currencies


async def test_known_usage_releases_difference_once_and_retains_price_snapshot(
    tmp_path: Path,
) -> None:
    budget = await setup_budget(tmp_path, cost_currency="CNY", max_daily_cost=0.001)
    permit = await budget.reserve(endpoint="fixture", tokens=400, final=True, pricing=PRICING)
    usage = ModelUsage(
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        usage_known=True,
        estimated_cost=99,
        cost_currency="USD",
        provider_request_id="fixture-receipt",
    )
    await asyncio.gather(*(budget.settle(permit.call_id, usage) for _ in range(8)))
    value = await row(budget, permit.call_id)
    assert (value.charged_micros, value.reserved_micros, value.currency) == (200, 800, "CNY")
    assert value.provider_request_id == "fixture-receipt"
    assert value.state == "estimated"
    await budget.reserve(endpoint="fixture", tokens=400, final=True, pricing=PRICING)
    with pytest.raises(BudgetDenied, match="cost_settlement_conflict"):
        await budget.settle(
            permit.call_id, usage.model_copy(update={"output_tokens": 60, "total_tokens": 160})
        )


@pytest.mark.parametrize(
    "pricing",
    [
        None,
        ModelPricing(input_rate=1, currency="CNY"),
        ModelPricing(input_rate=1, output_rate=2, currency="USD"),
    ],
)
async def test_enabled_money_limit_requires_usable_matching_price(
    tmp_path: Path, pricing: ModelPricing | None
) -> None:
    budget = await setup_budget(tmp_path, cost_currency="CNY", max_daily_cost=1)
    with pytest.raises(BudgetDenied, match="cost_pricing_unavailable"):
        await budget.reserve(endpoint="fixture", tokens=400, final=True, pricing=pricing)
    async with budget._database.sessions() as session:
        root = await session.get(TaskRunRecord, budget._run_id)
        assert root is not None and root.llm_attempts == root.budget_tokens == 0
        assert list(await session.scalars(select(ModelCostRecord))) == []


async def test_unknown_usage_survives_source_deletion_and_late_settlement(tmp_path: Path) -> None:
    budget = await setup_budget(tmp_path, cost_currency="CNY", max_daily_cost=0.001)
    permit = await budget.reserve(endpoint="fixture", tokens=400, final=True, pricing=PRICING)
    await budget.settle(permit.call_id, None)
    assert (await row(budget, permit.call_id)).charged_micros == 800
    async with budget._database.sessions.begin() as session:
        await session.execute(
            delete(ModelReservationRecord).where(ModelReservationRecord.run_id == budget._run_id)
        )
        await session.execute(delete(TaskRunRecord).where(TaskRunRecord.id == budget._run_id))
    await budget.settle(
        permit.call_id,
        ModelUsage(input_tokens=20, output_tokens=10, total_tokens=30, usage_known=True),
    )
    assert (await row(budget, permit.call_id)).charged_micros == 40
    async with budget._database.sessions() as session:
        assert await session.get(TaskRunRecord, budget._run_id) is None


async def test_monthly_limit_counts_previous_day_without_refunding_unknown(tmp_path: Path) -> None:
    budget = await setup_budget(
        tmp_path, cost_currency="CNY", max_daily_cost=1, max_monthly_cost=0.001
    )
    permit = await budget.reserve(endpoint="fixture", tokens=400, final=True, pricing=PRICING)
    now = datetime.now(UTC)
    # First day is a valid same-month boundary too; avoid pretending the previous
    # calendar month belongs to this month's limit.
    old = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if now.day > 1:
        old -= timedelta(days=1)
    async with budget._database.sessions.begin() as session:
        await session.execute(
            update(ModelCostRecord)
            .where(ModelCostRecord.call_id == permit.call_id)
            .values(created_at=old)
        )
    await budget.settle(permit.call_id, None)
    with pytest.raises(BudgetDenied, match="monthly_cost_budget_exhausted"):
        await budget.reserve(endpoint="fixture", tokens=200, final=True, pricing=PRICING)


async def test_new_limit_does_not_ignore_previously_unpriced_calls(tmp_path: Path) -> None:
    budget = await setup_budget(tmp_path)
    permit = await budget.reserve(endpoint="fixture", tokens=400, final=True)
    await budget.settle(permit.call_id, None)
    limited = RunModelBudget(
        budget._database,
        run_id=budget._run_id,
        user_id=budget._user_id,
        config=RunBudgetConfig(cost_currency="CNY", max_daily_cost=1),
    )
    with pytest.raises(BudgetDenied, match="cost_usage_unknown"):
        await limited.reserve(endpoint="fixture", tokens=400, final=True, pricing=PRICING)


async def test_snapshot_limit_survives_config_rollback_and_stale_recovery(tmp_path: Path) -> None:
    budget = await setup_budget(tmp_path, cost_currency="CNY", max_daily_cost=0.001)
    permit = await budget.reserve(endpoint="fixture", tokens=400, final=True, pricing=PRICING)
    async with budget._database.sessions.begin() as session:
        await session.execute(
            update(ModelCostRecord)
            .where(ModelCostRecord.call_id == permit.call_id)
            .values(created_at=datetime.now(UTC) - timedelta(seconds=1801))
        )
    await recover_stale_reservations(budget._database)
    assert (await row(budget, permit.call_id)).state == "unknown"
    assert (await row(budget, permit.call_id)).charged_micros == 800
    rolled_back = RunModelBudget(
        budget._database,
        run_id=budget._run_id,
        user_id=budget._user_id,
        config=RunBudgetConfig(cost_currency="CNY", max_daily_cost=1),
    )
    with pytest.raises(BudgetDenied, match="daily_cost_budget_exhausted"):
        await rolled_back.reserve(endpoint="fixture", tokens=200, final=True, pricing=PRICING)


async def test_explicit_zero_usage_settles_zero_but_default_usage_stays_unknown(
    tmp_path: Path,
) -> None:
    budget = await setup_budget(tmp_path, cost_currency="CNY", max_daily_cost=0.001)
    first = await budget.reserve(endpoint="fixture", tokens=400, final=True, pricing=PRICING)
    await budget.settle(first.call_id, ModelUsage(usage_known=True))
    assert (await row(budget, first.call_id)).charged_micros == 0
    async with budget._database.sessions() as session:
        reservation = await session.get(ModelReservationRecord, first.call_id)
        assert reservation is not None and reservation.state == "settled"
    second = await budget.reserve(endpoint="fixture", tokens=400, final=True, pricing=PRICING)
    await budget.settle(second.call_id, ModelUsage())
    assert (await row(budget, second.call_id)).charged_micros == 800


async def test_cancelled_required_work_denies_completed_parent_model_admission(
    tmp_path: Path,
) -> None:
    budget = await setup_budget(tmp_path)
    async with budget._database.sessions.begin() as session:
        await session.execute(
            update(TaskRunRecord)
            .where(TaskRunRecord.id == budget._run_id)
            .values(status="succeeded", contract={"work_cancel_requested": True})
        )
    maintenance = RunModelBudget(
        budget._database,
        run_id=budget._run_id,
        user_id=budget._user_id,
        config=RunBudgetConfig(),
        phase="maintenance",
    )
    with pytest.raises(BudgetDenied, match="budget_run_inactive"):
        await maintenance.reserve(endpoint="fixture", tokens=400, final=True, pricing=PRICING)


def test_money_limits_require_currency_without_invented_default() -> None:
    assert RunBudgetConfig().max_daily_cost is None
    with pytest.raises(ValidationError, match="cost_limit_requires_currency"):
        RunBudgetConfig(max_daily_cost=1)


async def test_router_denies_cost_before_provider_and_records_no_attempt(tmp_path: Path) -> None:
    from test_llm import FakeProvider, endpoint

    from app.harness.budget import budget_scope
    from app.llm import CompletionRequest, LLMRouter
    from app.llm.contracts import LLMMessage, LLMRoute, RoutePolicy

    budget = await setup_budget(tmp_path, cost_currency="CNY", max_daily_cost=0)
    model = endpoint(local=True).model_copy(
        update={"input_cost_per_million": 1, "output_cost_per_million": 2, "cost_currency": "CNY"}
    )
    provider = FakeProvider("fixture")
    router = LLMRouter(
        endpoints={"fixture": model},
        routes={name: RoutePolicy(primary="fixture") for name in LLMRoute},
        providers={"fixture": provider},
    )
    request = CompletionRequest(
        trace_id=uuid7(),
        messages=[LLMMessage(role="user", content="synthetic")],
        privacy_level="L1",
        route="dialogue",
    )
    with budget_scope(budget), pytest.raises(BudgetDenied, match="daily_cost_budget_exhausted"):
        await router.complete(request)
    assert provider.requests == []
    assert not (await cost_summary(budget._database, user_id=budget._user_id)).currencies


async def test_zero_money_limit_allows_explicit_free_model(tmp_path: Path) -> None:
    budget = await setup_budget(tmp_path, cost_currency="CNY", max_daily_cost=0, max_monthly_cost=0)
    permit = await budget.reserve(
        endpoint="free",
        tokens=400,
        final=True,
        pricing=ModelPricing(input_rate=0, output_rate=0, currency="CNY"),
    )
    await budget.settle(permit.call_id, None)
    assert (await row(budget, permit.call_id)).charged_micros == 0
    await budget.reserve(
        endpoint="free",
        tokens=400,
        final=True,
        pricing=ModelPricing(input_rate=0, output_rate=0, currency="CNY"),
    )


def test_money_limits_cannot_silently_disable_accounting() -> None:
    with pytest.raises(ValidationError, match="cost_limit_requires_enabled_budget"):
        RunBudgetConfig(enabled=False, cost_currency="CNY", max_daily_cost=1)


async def test_incomplete_usage_above_reservation_increases_unknown_hold(tmp_path: Path) -> None:
    budget = await setup_budget(tmp_path, cost_currency="CNY", max_daily_cost=0.001)
    permit = await budget.reserve(endpoint="fixture", tokens=400, final=True, pricing=PRICING)
    await budget.settle(permit.call_id, ModelUsage(total_tokens=1000))
    value = await row(budget, permit.call_id)
    assert value.charged_micros == 2000 and value.state == "unknown"
    with pytest.raises(BudgetDenied, match="daily_cost_budget_exhausted"):
        await budget.reserve(endpoint="fixture", tokens=1, final=True, pricing=PRICING)
