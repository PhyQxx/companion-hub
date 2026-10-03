"""UTC spending admission using decimal micro-units, never provider bills."""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.models import RunBudgetConfig
from app.db import Database, ModelCostRecord
from app.harness.budget import BudgetDenied
from app.harness.unit_costs import MAX_COST_MICROS
from app.llm.contracts import ModelPricing, ModelUsage
from app.schemas.costs import CostCurrencyView, CostSummaryView


def micros(value: float) -> int:
    with localcontext() as context:
        context.prec = 64
        return int((Decimal(str(value)) * 1_000_000).to_integral_value(rounding=ROUND_FLOOR))


def rate(value: float | None) -> Decimal | None:
    with localcontext() as context:
        context.prec = 64
        return (
            Decimal(str(value)).quantize(Decimal("0.000000000001"), rounding=ROUND_CEILING)
            if value is not None
            else None
        )


def charge(tokens: int, input_rate: Decimal | None, output_rate: Decimal | None) -> int | None:
    if input_rate is None or output_rate is None:
        return None
    # Rates per million tokens become micro currency units per token.
    with localcontext() as context:
        context.prec = 64
        return int(
            (tokens * max(input_rate, output_rate)).to_integral_value(rounding=ROUND_CEILING)
        )


def assert_cost_window(
    admitted_at: datetime, checked_at: datetime, policies: Iterable[RunBudgetConfig]
) -> None:
    for policy in policies:
        if (policy.max_daily_cost is not None and admitted_at.date() != checked_at.date()) or (
            policy.max_monthly_cost is not None
            and (admitted_at.year, admitted_at.month) != (checked_at.year, checked_at.month)
        ):
            raise BudgetDenied("cost_window_changed")


async def check_cost_allowance(
    session: AsyncSession,
    *,
    user_id: UUID,
    amount: int | None,
    currency: str | None,
    config: RunBudgetConfig,
    snapshot: RunBudgetConfig,
    now: datetime,
) -> None:
    policies = [
        policy
        for policy in (snapshot, config)
        if policy.max_daily_cost is not None or policy.max_monthly_cost is not None
    ]
    for policy in policies:
        if amount is None or currency != policy.cost_currency:
            raise BudgetDenied("cost_pricing_unavailable")
        for reason, cap, start in (
            (
                "daily_cost_budget_exhausted",
                policy.max_daily_cost,
                now.replace(hour=0, minute=0, second=0, microsecond=0),
            ),
            (
                "monthly_cost_budget_exhausted",
                policy.max_monthly_cost,
                now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
            ),
        ):
            if cap is None:
                continue
            # An unpriced historical call could belong to this currency. Do not
            # erase uncertainty by enabling a price/limit after that call.
            uncertain = await session.scalar(
                select(func.count())
                .select_from(ModelCostRecord)
                .where(
                    ModelCostRecord.user_id == user_id,
                    ModelCostRecord.created_at >= start,
                    or_(
                        ModelCostRecord.charged_micros.is_(None),
                        and_(
                            ModelCostRecord.currency.is_(None), ModelCostRecord.charged_micros > 0
                        ),
                    ),
                    or_(
                        ModelCostRecord.currency == policy.cost_currency,
                        ModelCostRecord.currency.is_(None),
                    ),
                )
            )
            if uncertain:
                raise BudgetDenied("cost_usage_unknown")
            amounts = await session.stream_scalars(
                select(ModelCostRecord.charged_micros).where(
                    ModelCostRecord.user_id == user_id,
                    ModelCostRecord.currency == policy.cost_currency,
                    ModelCostRecord.created_at >= start,
                )
            )
            # SQLite SUM over BIGINT can overflow across individually valid calls.
            # Python integers retain exact totals without NUMERIC/float conversion.
            held = 0
            async for value in amounts:
                held += value or 0
            if held + amount > micros(cap):
                raise BudgetDenied(reason)


async def reserve_cost(
    session: AsyncSession,
    *,
    call_id: UUID,
    user_id: UUID,
    endpoint: str,
    tokens: int,
    pricing: ModelPricing | None,
    config: RunBudgetConfig,
    snapshot: RunBudgetConfig,
    now: datetime,
) -> None:
    pricing = pricing or ModelPricing()
    input_rate, output_rate = rate(pricing.input_rate), rate(pricing.output_rate)
    reserved = charge(tokens, input_rate, output_rate)
    if reserved is not None and reserved > MAX_COST_MICROS:
        raise BudgetDenied("cost_reservation_overflow")
    await check_cost_allowance(
        session,
        user_id=user_id,
        amount=reserved,
        currency=pricing.currency,
        config=config,
        snapshot=snapshot,
        now=now,
    )
    session.add(
        ModelCostRecord(
            call_id=call_id,
            user_id=user_id,
            endpoint=endpoint,
            currency=pricing.currency,
            input_rate=input_rate,
            output_rate=output_rate,
            reserved_micros=reserved,
            charged_micros=reserved,
            state="reserved",
            created_at=now,
        )
    )


async def settle_cost(
    session: AsyncSession,
    *,
    call_id: UUID,
    user_id: UUID,
    usage: ModelUsage | None,
    now: datetime,
) -> bool:
    """Commit unknown on amount overflow; the caller rejects after its transaction."""
    row = await session.scalar(
        # Late usage survives source deletion, so its ledger must serialize
        # independently of the Run. SQLite ignores SELECT FOR UPDATE; start
        # with a write fence and inspect the current returned receipt instead.
        update(ModelCostRecord)
        .where(
            ModelCostRecord.call_id == call_id,
            ModelCostRecord.user_id == user_id,
        )
        .values(call_id=ModelCostRecord.call_id)
        .returning(ModelCostRecord)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    if row is None:
        return False  # Pre-ledger calls cannot be retroactively priced.
    if row.unit is not None:
        raise BudgetDenied("cost_kind_mismatch")
    if (
        usage is not None
        and usage.provider_request_id
        and row.provider_request_id
        and usage.provider_request_id != row.provider_request_id
    ):
        raise BudgetDenied("cost_receipt_conflict")
    actual = None
    if (
        usage is not None
        and usage.usage_known is not False
        and row.input_rate is not None
        and row.output_rate is not None
        and usage.total_tokens == usage.input_tokens + usage.output_tokens
        and (usage.total_tokens > 0 or usage.usage_known is True)
    ):
        with localcontext() as context:
            context.prec = 64
            actual = int(
                (
                    usage.input_tokens * row.input_rate + usage.output_tokens * row.output_rate
                ).to_integral_value(rounding=ROUND_CEILING)
            )
    if row.state == "estimated":
        if actual is not None and row.charged_micros != actual:
            raise BudgetDenied("cost_settlement_conflict")
        return False
    observed_hold = (
        charge(
            max(usage.total_tokens, usage.input_tokens + usage.output_tokens),
            row.input_rate,
            row.output_rate,
        )
        if usage is not None and actual is None
        else actual
    )
    overflow = observed_hold is not None and observed_hold > MAX_COST_MICROS
    row.state = "estimated" if actual is not None and not overflow else "unknown"
    if overflow:
        row.charged_micros = None
    elif actual is not None:
        row.charged_micros = actual
    elif observed_hold is not None and row.charged_micros is not None:
        # A partial smaller observation cannot erase a previous overflow.
        row.charged_micros = max(row.charged_micros, observed_hold)
    if usage is not None and usage.provider_request_id:
        row.provider_request_id = usage.provider_request_id
    row.settled_at = now
    return overflow


async def recover_cost_reservations(database: Database) -> None:
    async with database.sessions.begin() as session:
        await session.execute(
            update(ModelCostRecord)
            .where(
                ModelCostRecord.state == "reserved",
                ModelCostRecord.created_at < datetime.now(UTC) - timedelta(seconds=1800),
            )
            .values(state="unknown", settled_at=datetime.now(UTC))
        )


async def cost_summary(database: Database, *, user_id: UUID, days: int = 30) -> CostSummaryView:
    if not 1 <= days <= 366:
        raise ValueError("invalid_cost_period")
    now = datetime.now(UTC)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    async with database.sessions() as session:
        rows = await session.stream(
            select(
                ModelCostRecord.currency,
                ModelCostRecord.charged_micros,
                ModelCostRecord.state,
            ).where(
                ModelCostRecord.user_id == user_id,
                ModelCostRecord.created_at >= start,
                ModelCostRecord.created_at <= now,
            )
        )
        totals: dict[str | None, list[int]] = {}
        async for currency, amount, state in rows:
            counts = totals.setdefault(currency, [0, 0, 0, 0, 0])
            counts[0] += amount or 0
            counts[{"estimated": 1, "reserved": 2, "unknown": 3}[state]] += 1
            counts[4] += amount is None
    return CostSummaryView(
        period_start=start,
        period_end=now,
        currencies=[
            CostCurrencyView(
                currency=currency,
                charged_micros=str(counts[0]),
                estimated_calls=counts[1],
                reserved_calls=counts[2],
                unknown_calls=counts[3],
                unpriced_calls=counts[4],
            )
            for currency, counts in sorted(totals.items(), key=lambda item: item[0] or "")
        ],
    )
