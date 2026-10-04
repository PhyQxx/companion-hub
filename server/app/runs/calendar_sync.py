"""Own a calendar workflow and commit its mirrors with fees and terminal evidence."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.calendar.mirror import CalendarMirrorService, CalendarMirrorStats, MirrorOccurrence
from app.calendar.sync_requests import CalendarRequestRunner
from app.calendar.sync_sources import CalendarSyncSource
from app.config import ConfigStore, DatabaseConfigStore
from app.config.models import VoiceCostConfig
from app.db import Database
from app.harness.budget import BudgetDenied
from app.harness.operations import OperationPolicy
from app.harness.unit_costs import UnitCostQuote, UnitPricing
from app.schemas import PrivacyLevel

from .admin_operation import admin_tool_request
from .operation import operate_with_run

T = TypeVar("T")


@dataclass
class CalendarSyncBatch:
    calendar_id: str
    occurrences: dict[str, MirrorOccurrence]
    stats: Any
    successful_fetches: int = 0


class CalendarFetchFailed(RuntimeError):
    def __init__(self, batch: CalendarSyncBatch) -> None:
        super().__init__("calendar_sync_fetch_failed")
        self.batch = batch


class CalendarHttpRequests:
    def __init__(
        self,
        owner: UUID,
        provider: str,
        begin: Callable[[], Awaitable[None]],
        source: CalendarSyncSource,
    ) -> None:
        self._owner, self._provider, self._begin, self._source = owner, provider, begin, source

    async def __call__(self, method: str, invoke: Callable[[], Awaitable[T]]) -> T:
        return await admin_tool_request(
            owner=self._owner,
            tool_name=f"calendar.{self._provider}.http.{method.lower()}",
            invoke=invoke,
            source_guard=self._source.check,
            before_start=self._begin,
        )


async def owned_calendar_sync(
    database: Database,
    store: ConfigStore | DatabaseConfigStore,
    *,
    user_id: UUID,
    provider: str,
    price: VoiceCostConfig | None,
    source: CalendarSyncSource,
    mirror: CalendarMirrorService,
    fetch: Callable[[CalendarRequestRunner], Awaitable[CalendarSyncBatch]],
    now: datetime,
) -> CalendarSyncBatch:
    snapshot = store.current
    quote = (
        UnitCostQuote(
            pricing=UnitPricing(
                unit="request",
                currency=price.cost_currency,
                rate_per_unit=price.request_cost_ceiling,
            ),
            maximum_quantity=1,
        )
        if price is not None
        and price.cost_currency is not None
        and price.request_cost_ceiling is not None
        else None
    )
    counter: CalendarMirrorStats | None = None

    async def workflow(begin: Callable[[], Awaitable[None]]) -> CalendarSyncBatch:
        batch = await fetch(CalendarHttpRequests(user_id, provider, begin, source))
        if batch.stats.errors and batch.successful_fetches == 0:
            raise CalendarFetchFailed(batch)
        return batch

    async def commit(sql: AsyncSession, batch: CalendarSyncBatch) -> None:
        nonlocal counter
        counter = await mirror.apply_in_session(
            sql,
            user_id,
            batch.calendar_id,
            batch.occurrences,
            now=now,
            authoritative=not batch.stats.errors,
            source=source,
        )

    def evidence(batch: CalendarSyncBatch) -> dict[str, str]:
        assert counter is not None
        return {
            "calendar_mirror_committed": "true",
            "calendar_snapshot_complete": str(not batch.stats.errors).lower(),
            "calendar_error_count": str(len(batch.stats.errors)),
            "calendar_mirrors_created": str(counter.mirrors_created),
            "calendar_mirrors_updated": str(counter.mirrors_updated),
            "calendar_mirrors_cancelled": str(counter.mirrors_cancelled),
        }

    try:
        batch = await operate_with_run(
            database,
            OperationPolicy(
                snapshot.version, tuple(snapshot.config.run_budget.model_dump().items()), quote
            ),
            user_id=user_id,
            privacy_level=PrivacyLevel.L1,
            entry=f"calendar.{provider}.sync",
            invoke=workflow,
            evidence=evidence,
            source_guard=source.check,
            source_commit_guard=source.check_memory,
            cost_endpoint=f"calendar.{provider}.sync",
            budget_source=lambda: store.current.config.run_budget,
            cooperative=True,
            defer_watch=lambda: source.closing,
            terminal_write=commit,
            transaction_fence=source.commit_fence,
            missing_quote_reason="admin_cost_estimate_unavailable",
        )
    except CalendarFetchFailed as error:
        return error.batch
    except BudgetDenied:
        await source.check()
        raise
    assert counter is not None
    counter.merge_into(batch.stats)
    return batch
