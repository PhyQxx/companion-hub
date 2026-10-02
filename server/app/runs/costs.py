"""UTC spending admission using decimal micro-units, never provider bills."""

from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.models import RunBudgetConfig
from app.db import Database, ModelCostRecord
from app.harness.budget import BudgetDenied
from app.llm.contracts import ModelPricing, ModelUsage
from app.schemas.costs import CostCurrencyView, CostSummaryView


def micros(value: float) -> int:
    return int((Decimal(str(value)) * 1_000_000).to_integral_value(rounding=ROUND_FLOOR))


def rate(value: float | None) -> Decimal | None:
    return (
        Decimal(str(value)).quantize(Decimal("0.000000000001"), rounding=ROUND_CEILING)
        if value is not None
        else None
    )


def charge(tokens: int, input_rate: Decimal | None, output_rate: Decimal | None) -> int | None:
    if input_rate is None or output_rate is None:
        return None
    # Rates per million tokens become micro currency units per token.
    return int((tokens * max(input_rate, output_rate)).to_integral_value(rounding=ROUND_CEILING))


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
    policies = [
        policy
        for policy in (snapshot, config)
        if policy.max_daily_cost is not None or policy.max_monthly_cost is not None
    ]
    for policy in policies:
        if reserved is None or pricing.currency != policy.cost_currency:
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
            held = await session.scalar(
                select(func.sum(ModelCostRecord.charged_micros)).where(
                    ModelCostRecord.user_id == user_id,
                    ModelCostRecord.currency == policy.cost_currency,
                    ModelCostRecord.created_at >= start,
                )
            )
            if int(held or 0) + reserved > micros(cap):
                raise BudgetDenied(reason)
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
) -> None:
    row = await session.scalar(
        select(ModelCostRecord)
        .where(
            ModelCostRecord.call_id == call_id,
            ModelCostRecord.user_id == user_id,
        )
        .with_for_update()
    )
    if row is None:
        return  # Pre-ledger calls cannot be retroactively priced.
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
        actual = int(
            (
                usage.input_tokens * row.input_rate + usage.output_tokens * row.output_rate
            ).to_integral_value(rounding=ROUND_CEILING)
        )
    if row.state == "estimated":
        if actual is not None and row.charged_micros != actual:
            raise BudgetDenied("cost_settlement_conflict")
        return
    row.state = "estimated" if actual is not None else "unknown"
    if actual is not None:
        row.charged_micros = actual
    elif usage is not None:
        observed_hold = charge(
            max(usage.total_tokens, usage.input_tokens + usage.output_tokens),
            row.input_rate,
            row.output_rate,
        )
        if observed_hold is not None:
            row.charged_micros = max(row.charged_micros or 0, observed_hold)
    if usage is not None and usage.provider_request_id:
        row.provider_request_id = usage.provider_request_id
    row.settled_at = now


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
        rows = (
            await session.execute(
                select(
                    ModelCostRecord.currency,
                    func.coalesce(func.sum(ModelCostRecord.charged_micros), 0),
                    func.sum(case((ModelCostRecord.state == "estimated", 1), else_=0)),
                    func.sum(case((ModelCostRecord.state == "reserved", 1), else_=0)),
                    func.sum(case((ModelCostRecord.state == "unknown", 1), else_=0)),
                    func.sum(case((ModelCostRecord.charged_micros.is_(None), 1), else_=0)),
                )
                .where(
                    ModelCostRecord.user_id == user_id,
                    ModelCostRecord.created_at >= start,
                    ModelCostRecord.created_at <= now,
                )
                .group_by(ModelCostRecord.currency)
                .order_by(ModelCostRecord.currency)
            )
        ).all()
    return CostSummaryView(
        period_start=start,
        period_end=now,
        currencies=[
            CostCurrencyView(
                currency=row[0],
                charged_micros=str(row[1]),
                estimated_calls=int(row[2]),
                reserved_calls=int(row[3]),
                unknown_calls=int(row[4]),
                unpriced_calls=int(row[5]),
            )
            for row in rows
        ],
    )
