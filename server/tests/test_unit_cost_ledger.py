"""Synthetic unit calls share exact owned currency limits on both backends."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import delete, update
from test_run_budget import make_budget
from test_run_cancel_fence import prepared

from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, ModelCostRecord, TaskRunRecord
from app.harness.budget import BudgetDenied, CallPermit
from app.harness.unit_costs import UnitCostQuote, UnitUsage
from app.ids import uuid7
from app.llm.contracts import ModelPricing, ModelUsage
from app.runs.costs import cost_summary, settle_cost
from app.runs.unit_costs import UnitCostStore


def quote(
    rate: str = "0.001", maximum: str = "10", unit: str = "second", currency: str = "CNY"
) -> UnitCostQuote:
    return UnitCostQuote.model_validate(
        {
            "pricing": {"unit": unit, "currency": currency, "rate_per_unit": rate},
            "maximum_quantity": maximum,
        }
    )


async def read(store: UnitCostStore, call_id: UUID) -> ModelCostRecord:
    async with store._database.sessions() as session:
        return await session.get_one(ModelCostRecord, call_id)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize(
    "known,quantity,expected",
    [
        (True, "2", 2000),
        (True, "0", 0),
        (False, "0", 10000),
        (False, "12", 12000),
        (True, "12", 12000),
    ],
)
async def test_started_usage_retains_quote_and_exact_amount(
    backend: str,
    known: bool,
    quantity: str,
    expected: int,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        config = RunBudgetConfig(cost_currency="CNY", max_daily_cost=1)
        budget = await make_budget(storage.database, config)
        store, call = UnitCostStore(storage.database), uuid7()
        await store.reserve(
            call_id=call,
            user_id=budget._user_id,
            endpoint="synthetic",
            quote=quote(),
            config=config,
        )
        await store.mark_started(call_id=call, user_id=budget._user_id)
        usage = UnitUsage(
            quantity=Decimal(quantity), usage_known=known, provider_request_id="fixture-receipt"
        )
        await asyncio.gather(
            *(store.settle(call_id=call, user_id=budget._user_id, usage=usage) for _ in range(4))
        )
        row = await read(store, call)
        assert (row.charged_micros, row.reserved_micros) == (expected, 10000)
        assert row.state == ("estimated" if known else "unknown")
        assert (row.unit, row.unit_rate, row.unit_maximum_quantity, row.unit_quantity) == (
            "second",
            Decimal("0.001"),
            Decimal(10),
            Decimal(quantity),
        )
        assert row.input_rate is row.output_rate is None
        assert row.provider_request_id == "fixture-receipt"
        assert not await store.release_unstarted(call_id=call, user_id=budget._user_id)
        with pytest.raises(BudgetDenied, match="unit_cost_reservation_inactive"):
            await store.mark_started(call_id=call, user_id=budget._user_id)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_prestart_and_duplicate_calls_never_replay_or_invent_usage(
    backend: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        store, call = UnitCostStore(storage.database), uuid7()

        async def reserve(value: UnitCostQuote) -> None:
            await store.reserve(
                call_id=call,
                user_id=budget._user_id,
                endpoint="synthetic",
                quote=value,
                config=RunBudgetConfig(),
            )

        await reserve(quote())
        with pytest.raises(BudgetDenied, match="unit_cost_already_reserved"):
            await reserve(quote())
        with pytest.raises(BudgetDenied, match="cost_reservation_conflict"):
            await reserve(quote("0.002"))
        with pytest.raises(BudgetDenied, match="unit_cost_not_started"):
            await store.settle(
                call_id=call,
                user_id=budget._user_id,
                usage=UnitUsage(quantity=Decimal(0), usage_known=True),
            )
        assert (await read(store, call)).state == "reserved"
        assert await store.release_unstarted(call_id=call, user_id=budget._user_id)
        assert not await store.release_unstarted(call_id=call, user_id=budget._user_id)
        row = await read(store, call)
        assert (row.state, row.charged_micros, row.unit_quantity) == ("estimated", 0, Decimal(0))
        with pytest.raises(BudgetDenied, match="unit_cost_reservation_inactive"):
            await store.mark_started(call_id=call, user_id=budget._user_id)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_late_fee_after_source_deletion_and_observations_keep_uncertainty(
    backend: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        store, call = UnitCostStore(storage.database), uuid7()
        await store.reserve(
            call_id=call,
            user_id=budget._user_id,
            endpoint="synthetic",
            quote=quote(),
            config=RunBudgetConfig(),
        )
        await store.mark_started(call_id=call, user_id=budget._user_id)
        await store.settle(call_id=call, user_id=budget._user_id, usage=None)
        for quantity in (12, 2):
            await store.settle(
                call_id=call,
                user_id=budget._user_id,
                usage=UnitUsage(quantity=Decimal(quantity), usage_known=False),
            )
        row = await read(store, call)
        assert (row.state, row.charged_micros, row.unit_quantity) == ("unknown", 12000, Decimal(12))
        async with storage.database.sessions.begin() as session:
            await session.execute(delete(TaskRunRecord).where(TaskRunRecord.id == budget._run_id))
        await store.settle(
            call_id=call,
            user_id=budget._user_id,
            usage=UnitUsage(quantity=Decimal(1), usage_known=True),
        )
        assert (await read(store, call)).charged_micros == 1000
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_foreign_missing_inactive_owners_and_wrong_cost_kinds(
    backend: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        other = await make_budget(storage.database, RunBudgetConfig())
        store, call, missing = UnitCostStore(storage.database), uuid7(), uuid7()
        await store.reserve(
            call_id=call,
            user_id=budget._user_id,
            endpoint="synthetic",
            quote=quote(),
            config=RunBudgetConfig(),
        )
        await store.settle(call_id=call, user_id=other._user_id, usage=None)
        await store.settle(call_id=missing, user_id=budget._user_id, usage=None)
        assert not await store.release_unstarted(call_id=call, user_id=other._user_id)
        with pytest.raises(BudgetDenied, match="cost_reservation_conflict"):
            await store.reserve(
                call_id=call,
                user_id=other._user_id,
                endpoint="synthetic",
                quote=quote(),
                config=RunBudgetConfig(),
            )
        assert (await read(store, call)).state == "reserved"
        async with storage.database.sessions.begin() as session:
            with pytest.raises(BudgetDenied, match="cost_kind_mismatch"):
                await settle_cost(
                    session,
                    call_id=call,
                    user_id=budget._user_id,
                    usage=ModelUsage(usage_known=True),
                    now=datetime.now(UTC),
                )
        permit = await budget.reserve(
            endpoint="token-fixture",
            tokens=1,
            final=True,
            pricing=ModelPricing(input_rate=1, output_rate=1, currency="CNY"),
        )
        with pytest.raises(BudgetDenied, match="cost_kind_mismatch"):
            await store.settle(call_id=permit.call_id, user_id=budget._user_id, usage=None)
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(AppUserRecord)
                .where(AppUserRecord.id == budget._user_id)
                .values(status="disabled")
            )
        with pytest.raises(BudgetDenied, match="budget_owner_invalid"):
            await store.mark_started(call_id=call, user_id=budget._user_id)
        with pytest.raises(BudgetDenied, match="budget_owner_invalid"):
            await store.reserve(
                call_id=missing,
                user_id=budget._user_id,
                endpoint="synthetic",
                quote=quote(),
                config=RunBudgetConfig(),
            )
        async with storage.database.sessions() as session:
            assert await session.get(ModelCostRecord, missing) is None
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("kind", ["quantity", "receipt"])
async def test_competing_settlements_are_serialized_and_exact_known_usage_is_immutable(
    backend: str,
    kind: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        store, call = UnitCostStore(storage.database), uuid7()
        await store.reserve(
            call_id=call,
            user_id=budget._user_id,
            endpoint="synthetic",
            quote=quote("0"),
            config=RunBudgetConfig(),
        )
        await store.mark_started(call_id=call, user_id=budget._user_id)
        usages = [
            UnitUsage(
                quantity=Decimal(i if kind == "quantity" else 1),
                usage_known=True,
                provider_request_id=f"receipt-{i if kind == 'receipt' else 1}",
            )
            for i in (1, 2)
        ]
        results = await asyncio.gather(
            *(store.settle(call_id=call, user_id=budget._user_id, usage=usage) for usage in usages),
            return_exceptions=True,
        )
        assert sum(value is None for value in results) == 1
        refusal = next(value for value in results if isinstance(value, BudgetDenied))
        assert (
            refusal.reason_code
            == f"cost_{'settlement' if kind == 'quantity' else 'receipt'}_conflict"
        )
        row = await read(store, call)
        assert row.charged_micros == 0 and row.state == "estimated"
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_token_and_unit_reservations_share_owner_currency_limit(
    backend: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        config = RunBudgetConfig(cost_currency="CNY", max_daily_cost=0.01)
        budget = await make_budget(storage.database, config)
        store = UnitCostStore(storage.database)
        results = await asyncio.gather(
            *(
                store.reserve(
                    call_id=uuid7(),
                    user_id=budget._user_id,
                    endpoint="synthetic",
                    quote=quote("0.001", "6"),
                    config=config,
                )
                if i % 2
                else budget.reserve(
                    endpoint="token-fixture",
                    tokens=6000,
                    final=True,
                    pricing=ModelPricing(input_rate=1, output_rate=1, currency="CNY"),
                )
                for i in range(8)
            ),
            return_exceptions=True,
        )
        assert sum(value is None or isinstance(value, CallPermit) for value in results) == 1
        assert sum(isinstance(value, BudgetDenied) for value in results) == 7
        summary = await cost_summary(storage.database, user_id=budget._user_id)
        assert summary.currencies[0].charged_micros == "6000"
        assert summary.currencies[0].reserved_calls == 1
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("limit", ["snapshot", "current", "monthly", "currency", "historical"])
async def test_declared_currency_and_strictest_caps_cannot_be_bypassed(
    backend: str,
    limit: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        store, now = UnitCostStore(storage.database), datetime.now(UTC)
        loose = RunBudgetConfig(cost_currency="CNY", max_daily_cost=1)
        tight = RunBudgetConfig(cost_currency="CNY", max_daily_cost=0.001)
        current, snapshot, expected = loose, loose, "daily_cost_budget_exhausted"
        if limit == "snapshot":
            snapshot = tight
        elif limit == "current":
            current = tight
        elif limit == "monthly":
            current = RunBudgetConfig(cost_currency="CNY", max_monthly_cost=0.001)
            expected = "monthly_cost_budget_exhausted"
        elif limit == "currency":
            current = RunBudgetConfig(cost_currency="USD", max_daily_cost=1)
            expected = "cost_pricing_unavailable"
        else:
            async with storage.database.sessions.begin() as session:
                session.add(
                    ModelCostRecord(
                        call_id=uuid7(),
                        user_id=budget._user_id,
                        endpoint="old-unpriced",
                        state="unknown",
                        created_at=now,
                        charged_micros=None,
                    )
                )
            expected = "cost_usage_unknown"
        with pytest.raises(BudgetDenied, match=expected):
            await store.reserve(
                call_id=uuid7(),
                user_id=budget._user_id,
                endpoint="synthetic",
                quote=quote(),
                config=current,
                snapshot=snapshot,
            )
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_overflow_never_wraps_refunds_unknown_or_loses_low_decimal_digits(
    backend: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        store, call = UnitCostStore(storage.database), uuid7()
        precise = quote("999999999.000000000001", "1.000000000001")
        await store.reserve(
            call_id=call,
            user_id=budget._user_id,
            endpoint="synthetic",
            quote=precise,
            config=RunBudgetConfig(),
        )
        row = await read(store, call)
        assert row.unit_rate == precise.pricing.rate_per_unit
        assert row.unit_maximum_quantity == precise.maximum_quantity
        assert row.reserved_micros == 999999999001001
        await store.mark_started(call_id=call, user_id=budget._user_id)
        await store.settle(
            call_id=call,
            user_id=budget._user_id,
            usage=UnitUsage(quantity=Decimal("1000000000"), usage_known=False),
        )
        await store.settle(
            call_id=call,
            user_id=budget._user_id,
            usage=UnitUsage(quantity=Decimal(1), usage_known=False),
        )
        row = await read(store, call)
        assert row.state == "unknown" and row.charged_micros is None
        assert row.unit_quantity == Decimal("1000000000")
        with pytest.raises(BudgetDenied, match="cost_usage_unknown"):
            await store.reserve(
                call_id=uuid7(),
                user_id=budget._user_id,
                endpoint="synthetic",
                quote=quote(),
                config=RunBudgetConfig(cost_currency="CNY", max_daily_cost=1),
            )
        await store.settle(
            call_id=call,
            user_id=budget._user_id,
            usage=UnitUsage(quantity=Decimal(0), usage_known=True),
        )
        assert (await read(store, call)).charged_micros == 0
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_summary_and_caps_sum_more_than_database_int64_exactly(
    backend: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        store = UnitCostStore(storage.database)
        big = quote("1000000000", "5000")
        for _ in range(2):
            await store.reserve(
                call_id=uuid7(),
                user_id=budget._user_id,
                endpoint="synthetic",
                quote=big,
                config=RunBudgetConfig(),
            )
        summary = await cost_summary(storage.database, user_id=budget._user_id)
        assert summary.currencies[0].charged_micros == "10000000000000000000"
        with pytest.raises(BudgetDenied, match="daily_cost_budget_exhausted"):
            await store.reserve(
                call_id=uuid7(),
                user_id=budget._user_id,
                endpoint="synthetic",
                quote=quote(),
                config=RunBudgetConfig(cost_currency="CNY", max_daily_cost=1),
            )
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_old_day_is_counted_monthly_and_other_owner_currency_is_separate(
    backend: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        config = RunBudgetConfig(cost_currency="CNY", max_daily_cost=0.01, max_monthly_cost=0.01)
        budget = await make_budget(storage.database, config)
        now = datetime(2026, 10, 4, 12, tzinfo=UTC)
        store, call = UnitCostStore(storage.database, clock=lambda: now), uuid7()
        await store.reserve(
            call_id=call,
            user_id=budget._user_id,
            endpoint="synthetic",
            quote=quote(),
            config=config,
        )
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(ModelCostRecord)
                .where(ModelCostRecord.call_id == call)
                .values(created_at=now - timedelta(days=1))
            )
        with pytest.raises(BudgetDenied, match="monthly_cost_budget_exhausted"):
            await store.reserve(
                call_id=uuid7(),
                user_id=budget._user_id,
                endpoint="synthetic",
                quote=quote(),
                config=config,
            )
        await store.reserve(
            call_id=uuid7(),
            user_id=budget._user_id,
            endpoint="usd-fixture",
            quote=quote(currency="USD"),
            config=RunBudgetConfig(),
        )
        other = await make_budget(storage.database, config)
        await store.reserve(
            call_id=uuid7(),
            user_id=other._user_id,
            endpoint="synthetic",
            quote=quote(),
            config=config,
        )
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_discrete_unit_cannot_settle_fractional_usage(backend: str, tmp_path: Path) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        store, call = UnitCostStore(storage.database), uuid7()
        await store.reserve(
            call_id=call,
            user_id=budget._user_id,
            endpoint="synthetic",
            quote=quote(unit="image"),
            config=RunBudgetConfig(),
        )
        await store.mark_started(call_id=call, user_id=budget._user_id)
        with pytest.raises(BudgetDenied, match="unit_cost_quantity_must_be_integral"):
            await store.settle(
                call_id=call,
                user_id=budget._user_id,
                usage=UnitUsage(quantity=Decimal("0.5"), usage_known=True),
            )
    finally:
        await storage.close()


async def test_explicit_sqlite_transaction_serializes_eight_unit_reservations(
    tmp_path: Path,
) -> None:
    from test_delivery_sqlite_transactions import explicit_transactions

    storage = await prepared("sqlite", tmp_path)
    try:
        config = RunBudgetConfig(cost_currency="CNY", max_daily_cost=0.001)
        budget = await make_budget(storage.database, config)
        store = UnitCostStore(storage.database)
        async with explicit_transactions(storage.database):
            results = await asyncio.gather(
                *(
                    store.reserve(
                        call_id=uuid7(),
                        user_id=budget._user_id,
                        endpoint="synthetic",
                        quote=quote("0.001", "1"),
                        config=config,
                    )
                    for _ in range(8)
                ),
                return_exceptions=True,
            )
        assert sum(value is None for value in results) == 1
        assert sum(isinstance(value, BudgetDenied) for value in results) == 7
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_stale_unit_reservations_keep_fee_and_cannot_start_or_refund(
    backend: str,
    tmp_path: Path,
) -> None:
    from app.runs.costs import recover_cost_reservations

    storage = await prepared(backend, tmp_path)
    try:
        budget = await make_budget(storage.database, RunBudgetConfig())
        old = datetime.now(UTC) - timedelta(seconds=1801)
        store, call = UnitCostStore(storage.database, clock=lambda: old), uuid7()
        await store.reserve(
            call_id=call,
            user_id=budget._user_id,
            endpoint="synthetic",
            quote=quote(),
            config=RunBudgetConfig(),
        )
        await recover_cost_reservations(storage.database)
        assert not await store.release_unstarted(call_id=call, user_id=budget._user_id)
        with pytest.raises(BudgetDenied, match="unit_cost_reservation_inactive"):
            await store.mark_started(call_id=call, user_id=budget._user_id)
        row = await read(store, call)
        assert (row.state, row.charged_micros) == ("unknown", 10000)
    finally:
        await storage.close()
