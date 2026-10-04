"""Own authorization code HTTP, fees and token installation together."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.calendar.google import GoogleCalendarClient, GoogleTokenStore
from app.calendar.oauth_sources import GoogleOAuthSource
from app.config import ConfigStore, DatabaseConfigStore
from app.config.models import GoogleCalendarConfig
from app.db import CalendarOAuthStateRecord, Database
from app.harness.budget import BudgetDenied
from app.harness.joined_read import join_on_cancel
from app.harness.operations import OperationPolicy
from app.harness.unit_costs import UnitCostQuote, UnitPricing
from app.schemas import PrivacyLevel

from .calendar_sync import CalendarHttpRequests
from .operation import operate_with_run


def new_oauth_http(timeout_seconds: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout_seconds,
        transport=httpx.AsyncHTTPTransport(retries=0),
        trust_env=False,
        follow_redirects=False,
    )


async def exchange_google_authorization(
    database: Database,
    store: ConfigStore | DatabaseConfigStore,
    *,
    source: GoogleOAuthSource,
    config: GoogleCalendarConfig,
    secret: str,
    code: str,
) -> tuple[str, str | None]:
    price = config.code_exchange_cost
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
    snapshot = store.current
    tokens = GoogleTokenStore(database)

    async def workflow(begin: Callable[[], Awaitable[None]]) -> tuple[str, str | None]:
        assert config.client_id is not None and config.redirect_uri is not None
        http = new_oauth_http(config.timeout_seconds)
        try:
            return await source.call(
                partial(
                    GoogleCalendarClient.exchange_code,
                    client_id=config.client_id,
                    client_secret=secret,
                    code=code,
                    redirect_uri=config.redirect_uri,
                    client=http,
                    request_runner=CalendarHttpRequests(
                        source.owner, "google.oauth", begin, source
                    ),
                )
            )
        finally:
            source.closing = True
            try:
                await join_on_cancel(http.aclose(), name="google-oauth-close")
            finally:
                source.closing = False

    async def commit(sql: AsyncSession, result: tuple[str, str | None]) -> None:
        await source.validate(sql, lock=True)
        await tokens.save_in_session(
            sql, source.owner, *result, authorization_id=source.authorization_id
        )
        row = await sql.get_one(CalendarOAuthStateRecord, source.authorization_id)
        row.completed_at = datetime.now(UTC)

    try:
        return await operate_with_run(
            database,
            OperationPolicy(
                snapshot.version, tuple(snapshot.config.run_budget.model_dump().items()), quote
            ),
            user_id=source.owner,
            privacy_level=PrivacyLevel.L1,
            entry="calendar.google.oauth",
            invoke=workflow,
            evidence=lambda _: {"google_oauth_token_committed": "true"},
            source_guard=source.check,
            source_commit_guard=source.check_memory,
            cost_endpoint="calendar.google.oauth",
            budget_source=lambda: store.current.config.run_budget,
            cooperative=True,
            defer_watch=lambda: source.closing,
            terminal_write=commit,
            transaction_fence=source.commit_fence,
            missing_quote_reason="admin_cost_estimate_unavailable",
        )
    except BudgetDenied:
        await source.check()
        raise
