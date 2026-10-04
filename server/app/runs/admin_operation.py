"""Owned admin SDK requests against the account created by authenticated setup."""

from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy import select

from app.config import DatabaseConfigStore
from app.config.models import SenseAudioOperation, VoiceCostConfig
from app.db import AppUserRecord, AuthCredentialRecord, Database
from app.harness.budget import BudgetDenied, current_tool_budget
from app.harness.joined_read import join_on_cancel
from app.harness.operations import OperationPolicy
from app.harness.source_cleanup import close_after_source
from app.harness.unit_costs import UnitCostQuote, UnitPricing
from app.schemas import PrivacyLevel

from .operation import operate_with_run

T = TypeVar("T")


async def admin_sdk_request(
    database: Database,
    store: DatabaseConfigStore,
    *,
    operation: SenseAudioOperation,
    price: VoiceCostConfig | None,
    endpoint: str,
    invoke: Callable[[], Awaitable[T]],
    source_guard: Callable[[], Awaitable[None]],
) -> T:
    await source_guard()
    async with database.sessions() as session:
        owner = await session.scalar(
            select(AppUserRecord.id)
            .join(AuthCredentialRecord, AuthCredentialRecord.user_id == AppUserRecord.id)
            .where(AuthCredentialRecord.setup_slot == 1, AppUserRecord.status == "active")
        )
    if owner is None:
        raise BudgetDenied("admin_account_setup_required")
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

    async def request(mark_started: Callable[[], Awaitable[None]]) -> T:
        tool = current_tool_budget()
        permit = (
            await tool.reserve_tool(tool_name=f"admin.senseaudio.{operation}", user_id=owner)
            if tool
            else None
        )
        started = succeeded = False

        async def settle() -> None:
            if tool is not None and permit is not None:
                await join_on_cancel(
                    tool.settle_tool(
                        permit.call_id,
                        reported_ok=True if succeeded else None if started else False,
                    ),
                    name="admin-sdk-tool-settle",
                )

        async with close_after_source(settle):
            await mark_started()
            await source_guard()
            started = True
            result = await invoke()
            succeeded = True
            return result

    return await operate_with_run(
        database,
        OperationPolicy(
            snapshot.version, tuple(snapshot.config.run_budget.model_dump().items()), quote
        ),
        user_id=owner,
        privacy_level=PrivacyLevel.L1,
        entry=f"admin.senseaudio.{operation}",
        invoke=request,
        evidence=lambda _: {"source_actor": "admin", "response_received": "true"},
        source_guard=source_guard,
        # This callback checks only in-memory config and the bearer guard. It
        # must not acquire SQL resources while the authority locks are held.
        source_commit_guard=source_guard,
        cost_endpoint=endpoint,
        budget_source=lambda: store.current.config.run_budget,
        cooperative=True,
        missing_quote_reason="voice_cost_estimate_unavailable",
    )
