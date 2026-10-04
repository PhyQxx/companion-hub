"""Content-free per-attempt costs for an accepted Run whose root disabled quotas."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import update

from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, Database, ModelCostRecord, ModelReservationRecord, TaskRunRecord
from app.db.claims import assert_current_claim
from app.harness.budget import MAX_MODEL_TOKENS, BudgetDenied, CallPermit
from app.harness.joined_read import join_on_cancel
from app.harness.run_trace import DisabledRunTrace
from app.harness.time import utc
from app.ids import uuid7
from app.llm.contracts import ModelPricing, ModelUsage
from app.schemas import PrivacyLevel

from .costs import reserve_cost, settle_cost
from .trace_sources import (
    capture_disabled_trace,
    check_trace_binding,
    require_run_trace,
    validate_disabled_trace,
)


class DisabledModelAccounting:
    def __init__(
        self,
        database: Database,
        *,
        run_id: UUID,
        user_id: UUID,
        trace: DisabledRunTrace | None = None,
        maintenance: bool = False,
    ) -> None:
        self._database, self._run_id, self._user_id = database, run_id, user_id
        self._trace, self._maintenance = trace, maintenance
        self._capture_lock = asyncio.Lock()
        self._calls: set[UUID] = set()

    async def _source(self) -> DisabledRunTrace:
        async with self._capture_lock:
            if self._trace is None:
                self._trace = await capture_disabled_trace(
                    self._database,
                    self._run_id,
                    self._user_id,
                    maintenance=self._maintenance,
                    require_disabled=True,
                )
            assert self._trace is not None
            check_trace_binding(self._trace, self._database, self._user_id)
            if self._trace.run_id != self._run_id:
                raise BudgetDenied("run_trace_changed")
            return self._trace

    async def reserve(
        self, *, endpoint: str, tokens: int, pricing: ModelPricing, privacy_level: PrivacyLevel
    ) -> CallPermit:
        if not 1 <= tokens <= MAX_MODEL_TOKENS:
            raise ValueError("invalid_budget_reservation")
        trace = await self._source()
        call_id = uuid7()
        deadlines = [utc(trace.expires_at)] if trace.expires_at is not None else []
        async with self._database.sessions.begin() as sql:
            # Match the model-budget and worker lock order: Job -> owner -> Run.
            await assert_current_claim(sql)
            owner = await sql.scalar(
                update(AppUserRecord)
                .where(AppUserRecord.id == self._user_id, AppUserRecord.status == "active")
                .values(id=AppUserRecord.id)
                .returning(AppUserRecord.id)
            )
            if owner is None:
                raise BudgetDenied("budget_owner_invalid")
            await require_run_trace(
                sql, trace, user_id=self._user_id, privacy_level=privacy_level, lock=True
            )
            root = await sql.get_one(TaskRunRecord, trace.sources[0].run_id)
            if not root.budget or root.budget.get("enabled") is not False:
                raise BudgetDenied("run_trace_changed")
            now = datetime.now(UTC)
            # Quotas, including monetary caps, remain disabled for this root.
            # Keep prices and unknown holds so a future enabled policy can see them.
            disabled = RunBudgetConfig(enabled=False)
            await reserve_cost(
                sql,
                call_id=call_id,
                user_id=self._user_id,
                endpoint=endpoint,
                tokens=tokens,
                pricing=pricing,
                config=disabled,
                snapshot=disabled,
                now=now,
            )
            sql.add(
                ModelReservationRecord(
                    call_id=call_id,
                    run_id=self._run_id,
                    phase="maintenance" if self._maintenance else "interactive",
                    endpoint=endpoint,
                    state="reserved",
                    reserved_tokens=tokens,
                    charged_tokens=tokens,
                    created_at=now,
                )
            )
            await sql.flush()
            for ref in trace.sources:
                row = await sql.get(TaskRunRecord, ref.run_id)
                if (
                    row is not None
                    and row.deadline is not None
                    and not (ref.allow_succeeded and row.status == "succeeded")
                ):
                    deadlines.append(utc(row.deadline))
            await require_run_trace(sql, trace, user_id=self._user_id, privacy_level=privacy_level)
        self._calls.add(call_id)
        try:
            await validate_disabled_trace(self._database, trace)
        except BaseException:
            # The permit never reached a provider. This differs from unknown
            # usage after a permit has been handed to the router.
            await join_on_cancel(self._not_started(call_id), name="model-fee-not-started")
            raise
        remaining = (
            max(0.0, (min(deadlines) - datetime.now(UTC)).total_seconds())
            if deadlines
            else float("inf")
        )
        return CallPermit(call_id, remaining)

    async def _not_started(self, call_id: UUID) -> None:
        async with self._database.sessions.begin() as sql:
            await sql.execute(
                update(ModelReservationRecord)
                .where(ModelReservationRecord.call_id == call_id)
                .values(
                    state="settled", charged_tokens=0, actual_tokens=0, settled_at=datetime.now(UTC)
                )
            )
            await sql.execute(
                update(ModelCostRecord)
                .where(ModelCostRecord.call_id == call_id, ModelCostRecord.user_id == self._user_id)
                .values(state="estimated", charged_micros=0, settled_at=datetime.now(UTC))
            )

    async def settle(self, call_id: UUID, usage: ModelUsage | None) -> None:
        if call_id not in self._calls:
            raise BudgetDenied("cost_call_not_owned")
        actual = (
            max(usage.total_tokens, usage.input_tokens + usage.output_tokens)
            if usage is not None
            and usage.usage_known is not False
            and (usage.total_tokens > 0 or usage.usage_known is True)
            else None
        )
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as sql:
            # Match Run -> reservation -> fee recovery ordering. The fee survives
            # source deletion and must be settled even after authority was revoked.
            run = await sql.scalar(
                update(TaskRunRecord)
                .where(TaskRunRecord.id == self._run_id, TaskRunRecord.user_id == self._user_id)
                .values(id=TaskRunRecord.id)
                .returning(TaskRunRecord)
                .execution_options(synchronize_session=False, populate_existing=True)
            )
            reservation = await sql.scalar(
                update(ModelReservationRecord)
                .where(
                    ModelReservationRecord.call_id == call_id,
                    ModelReservationRecord.run_id == self._run_id,
                )
                .values(call_id=ModelReservationRecord.call_id)
                .returning(ModelReservationRecord)
                .execution_options(synchronize_session=False, populate_existing=True)
            )
            overflow = await settle_cost(
                sql, call_id=call_id, user_id=self._user_id, usage=usage, now=now
            )
            if actual is not None and actual > MAX_MODEL_TOKENS:
                overflow = True
            if reservation is not None:
                if (
                    reservation.state == "settled"
                    and actual is not None
                    and actual != reservation.actual_tokens
                ):
                    raise BudgetDenied("budget_settlement_conflict")
                if reservation.state != "settled":
                    reservation.state = (
                        "settled" if actual is not None and not overflow else "unknown"
                    )
                if reservation.state != "settled" or actual is not None:
                    reservation.actual_tokens = (
                        actual if actual is not None and not overflow else None
                    )
                if actual is not None and not overflow:
                    reservation.charged_tokens = actual
                reservation.settled_at = now
            if run is not None and overflow:
                run.contract = {**run.contract, "budget_usage_overflow": True}
        if overflow:
            raise BudgetDenied("budget_usage_overflow")
        await validate_disabled_trace(self._database, await self._source())
