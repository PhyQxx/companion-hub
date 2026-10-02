"""Owner-scoped, bounded read of the accounting ledger."""

from uuid import UUID

from sqlalchemy import select

from app.db import Database, ModelCostRecord
from app.harness.billing import compare_bill
from app.schemas.billing import BillingEvidence, BillingReport, CostPeriod, CostSnapshot, LedgerLine


async def cost_snapshot(database: Database, *, user_id: UUID, period: CostPeriod) -> CostSnapshot:
    async with database.sessions() as session:
        rows = list(
            await session.scalars(
                select(ModelCostRecord)
                .where(
                    ModelCostRecord.user_id == user_id,
                    ModelCostRecord.created_at >= period.period_start,
                    ModelCostRecord.created_at < period.period_end,
                )
                .order_by(ModelCostRecord.call_id)
                .limit(10001)
            )
        )
    if len(rows) > 10000:
        raise ValueError("billing_period_too_large")
    return CostSnapshot(
        period_start=period.period_start,
        period_end=period.period_end,
        lines=[
            LedgerLine(
                call_id=row.call_id,
                endpoint=row.endpoint,
                provider_request_id=row.provider_request_id,
                currency=row.currency,
                charged_micros=str(row.charged_micros) if row.charged_micros is not None else None,
                state=row.state,
            )
            for row in rows
        ],
    )


async def reconcile_bill(
    database: Database, *, user_id: UUID, evidence: BillingEvidence
) -> BillingReport:
    snapshot = await cost_snapshot(database, user_id=user_id, period=evidence)
    return compare_bill(snapshot, evidence)
