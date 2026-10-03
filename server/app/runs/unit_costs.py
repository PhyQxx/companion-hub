"""Owned unit reservations share the content-independent currency ledger."""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, Database, ModelCostRecord
from app.db.claims import assert_current_claim
from app.harness.budget import BudgetDenied
from app.harness.time import utc
from app.harness.unit_costs import MAX_COST_MICROS, UnitCostQuote, UnitUsage, unit_charge

from .costs import assert_cost_window, check_cost_allowance


async def lock_unit_cost(
    session: AsyncSession,
    *,
    call_id: UUID,
    user_id: UUID,
) -> ModelCostRecord | None:
    return cast(
        ModelCostRecord | None,
        await session.scalar(
            update(ModelCostRecord)
            .where(ModelCostRecord.call_id == call_id, ModelCostRecord.user_id == user_id)
            .values(call_id=ModelCostRecord.call_id)
            .returning(ModelCostRecord)
            .execution_options(synchronize_session=False, populate_existing=True)
        ),
    )


async def reserve_unit_cost(
    session: AsyncSession,
    *,
    call_id: UUID,
    user_id: UUID,
    endpoint: str,
    quote: UnitCostQuote,
    config: RunBudgetConfig,
    snapshot: RunBudgetConfig,
    now: datetime,
    clock: Callable[[], datetime] | None = None,
) -> datetime:
    if not endpoint or len(endpoint) > 160:
        raise ValueError("invalid_cost_endpoint")
    # Claim -> active owner -> ledger. This is also a real SQLite first write.
    await assert_current_claim(session)
    owner = await session.scalar(
        update(AppUserRecord)
        .where(AppUserRecord.id == user_id, AppUserRecord.status == "active")
        .values(id=AppUserRecord.id)
        .returning(AppUserRecord.id)
    )
    if owner is None:
        raise BudgetDenied("budget_owner_invalid")
    existing = await lock_unit_cost(session, call_id=call_id, user_id=user_id)
    if existing is not None:
        if (
            existing.unit != quote.pricing.unit
            or existing.currency != quote.pricing.currency
            or existing.unit_rate != quote.pricing.rate_per_unit
            or existing.unit_maximum_quantity != quote.maximum_quantity
            or existing.endpoint != endpoint
        ):
            raise BudgetDenied("cost_reservation_conflict")
        raise BudgetDenied("unit_cost_already_reserved")
    # A pre-lock timestamp can belong to the previous UTC spending period.
    current_time = clock or (lambda: now)
    admitted_at = utc(current_time())
    amount = unit_charge(quote.pricing.rate_per_unit, quote.maximum_quantity)
    await check_cost_allowance(
        session,
        user_id=user_id,
        amount=amount,
        currency=quote.pricing.currency,
        config=config,
        snapshot=snapshot,
        now=admitted_at,
    )
    session.add(
        ModelCostRecord(
            call_id=call_id,
            user_id=user_id,
            endpoint=endpoint,
            currency=quote.pricing.currency,
            unit=quote.pricing.unit,
            unit_rate=quote.pricing.rate_per_unit,
            unit_maximum_quantity=quote.maximum_quantity,
            reserved_micros=amount,
            charged_micros=amount,
            state="reserved",
            created_at=admitted_at,
        )
    )
    await session.flush()
    assert_cost_window(admitted_at, utc(current_time()), (config, snapshot))
    return admitted_at


async def settle_unit_cost(
    session: AsyncSession,
    *,
    call_id: UUID,
    user_id: UUID,
    usage: UnitUsage | None,
    now: datetime,
) -> None:
    row = await lock_unit_cost(session, call_id=call_id, user_id=user_id)
    if row is None:
        return  # Old calls and foreign callers cannot acquire invented pricing.
    if row.unit is None or row.unit_rate is None:
        raise BudgetDenied("cost_kind_mismatch")
    if usage is not None:
        if row.state == "reserved":
            raise BudgetDenied("unit_cost_not_started")
        if row.unit != "second" and usage.quantity % 1:
            raise BudgetDenied("unit_cost_quantity_must_be_integral")
    if (
        usage is not None
        and usage.provider_request_id
        and row.provider_request_id
        and (usage.provider_request_id != row.provider_request_id)
    ):
        raise BudgetDenied("cost_receipt_conflict")
    observed = unit_charge(row.unit_rate, usage.quantity) if usage is not None else None
    actual = observed if usage is not None and usage.usage_known else None
    overflow = observed is not None and observed > MAX_COST_MICROS
    if row.state == "estimated":
        if (
            actual is not None
            and usage is not None
            and (row.charged_micros != actual or row.unit_quantity != usage.quantity)
        ):
            raise BudgetDenied("cost_settlement_conflict")
        return
    row.state = "estimated" if actual is not None and not overflow else "unknown"
    if overflow:
        row.charged_micros = None  # Preserve ambiguity, never saturate or wrap an amount.
    elif actual is not None:
        row.charged_micros = actual
    elif observed is not None and row.charged_micros is not None:
        row.charged_micros = max(row.charged_micros, observed)
    if usage is not None:
        row.unit_quantity = (
            usage.quantity
            if actual is not None
            else max(row.unit_quantity or Decimal(0), usage.quantity)
        )
        if usage.provider_request_id:
            row.provider_request_id = usage.provider_request_id
    row.settled_at = now


class UnitCostStore:
    def __init__(self, database: Database, *, clock: Callable[[], datetime] | None = None) -> None:
        self._database = database
        self._clock = clock or (lambda: datetime.now(UTC))

    async def reserve(
        self,
        *,
        call_id: UUID,
        user_id: UUID,
        endpoint: str,
        quote: UnitCostQuote,
        config: RunBudgetConfig,
        snapshot: RunBudgetConfig | None = None,
    ) -> None:
        try:
            async with self._database.sessions.begin() as session:
                admitted_at = await reserve_unit_cost(
                    session,
                    call_id=call_id,
                    user_id=user_id,
                    endpoint=endpoint,
                    quote=quote,
                    config=config,
                    snapshot=snapshot or config,
                    now=utc(self._clock()),
                    clock=self._clock,
                )
        except IntegrityError as error:
            raise BudgetDenied("cost_reservation_conflict") from error
        try:
            assert_cost_window(admitted_at, utc(self._clock()), (config, snapshot or config))
        except BudgetDenied:
            # This facade has not returned admission to its caller yet.
            await self.release_unstarted(call_id=call_id, user_id=user_id)
            raise

    async def mark_started(self, *, call_id: UUID, user_id: UUID) -> None:
        async with self._database.sessions.begin() as session:
            await assert_current_claim(session)
            owner = await session.scalar(
                update(AppUserRecord)
                .where(AppUserRecord.id == user_id, AppUserRecord.status == "active")
                .values(id=AppUserRecord.id)
                .returning(AppUserRecord.id)
            )
            if owner is None:
                raise BudgetDenied("budget_owner_invalid")
            row = await lock_unit_cost(session, call_id=call_id, user_id=user_id)
            if row is None or row.unit is None or row.state != "reserved":
                raise BudgetDenied("unit_cost_reservation_inactive")
            row.state = "unknown"

    async def release_unstarted(self, *, call_id: UUID, user_id: UUID) -> bool:
        async with self._database.sessions.begin() as session:
            return (
                await session.scalar(
                    update(ModelCostRecord)
                    .where(
                        ModelCostRecord.call_id == call_id,
                        ModelCostRecord.user_id == user_id,
                        ModelCostRecord.unit.is_not(None),
                        ModelCostRecord.state == "reserved",
                    )
                    .values(
                        state="estimated",
                        charged_micros=0,
                        unit_quantity=0,
                        settled_at=utc(self._clock()),
                    )
                    .returning(ModelCostRecord.call_id)
                )
                is not None
            )

    async def settle(self, *, call_id: UUID, user_id: UUID, usage: UnitUsage | None) -> None:
        async with self._database.sessions.begin() as session:
            await settle_unit_cost(
                session,
                call_id=call_id,
                user_id=user_id,
                usage=usage,
                now=utc(self._clock()),
            )
