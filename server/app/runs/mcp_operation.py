"""Own complete MCP connections and admit each actual HTTP send."""

from collections.abc import Awaitable, Callable
from typing import Literal, TypeVar
from uuid import UUID

from sqlalchemy import select

from app.config import ConfigStore, DatabaseConfigStore
from app.config.models import McpServerConfig
from app.db import AppUserRecord, AuthCredentialRecord, Database
from app.harness.budget import BudgetDenied, current_tool_budget
from app.harness.operations import OperationPolicy
from app.harness.run_trace import current_run_trace
from app.harness.unit_costs import UnitCostQuote, UnitPricing
from app.integrations.mcp.models import McpCallPayload
from app.integrations.mcp.ports import request_runner_scope
from app.schemas import PrivacyLevel

from .admin_operation import admin_tool_request
from .operation import operate_with_run
from .resources import RunToolBudget

T = TypeVar("T")


class McpHttpRequests:
    def __init__(
        self,
        owner: UUID,
        begin: Callable[[], Awaitable[None]],
        guard: Callable[[], Awaitable[None]],
    ) -> None:
        self._owner, self._begin, self._guard = owner, begin, guard
        self.denied: BudgetDenied | None = None

    async def __call__(self, method: str, invoke: Callable[[], Awaitable[T]]) -> T:
        try:
            return await admin_tool_request(
                owner=self._owner,
                tool_name=f"mcp.http.{method.lower()}",
                invoke=invoke,
                source_guard=self._guard,
                before_start=self._begin,
            )
        except BudgetDenied as error:
            self.denied = self.denied or error
            raise


class OwnedMcpOperation:
    def __init__(self, database: Database, store: ConfigStore | DatabaseConfigStore) -> None:
        self._database = database
        self._store = store

    async def __call__(
        self,
        config: McpServerConfig,
        operation: Literal["refresh", "read", "write"],
        invoke: Callable[[], Awaitable[T]],
        source_guard: Callable[[], Awaitable[None]],
        closing: Callable[[], bool],
        *,
        user_id: UUID | None = None,
    ) -> T:
        await source_guard()
        tool, trace = current_tool_budget(), current_run_trace()
        if tool is not None and not isinstance(tool, RunToolBudget):
            raise BudgetDenied("budget_owner_invalid")
        parent_owner = (
            tool.owner_id if isinstance(tool, RunToolBudget) else trace.user_id if trace else None
        )
        if user_id is not None and parent_owner is not None and user_id != parent_owner:
            raise BudgetDenied("budget_owner_invalid")
        owner = user_id or parent_owner
        if owner is None:
            if operation != "refresh":
                raise BudgetDenied("mcp_request_owner_missing")
            async with self._database.sessions() as sql:
                owner = await sql.scalar(
                    select(AppUserRecord.id)
                    .join(AuthCredentialRecord, AuthCredentialRecord.user_id == AppUserRecord.id)
                    .where(AuthCredentialRecord.setup_slot == 1, AppUserRecord.status == "active")
                )
        if owner is None:
            raise BudgetDenied("admin_account_setup_required")
        snapshot = self._store.current
        price = config.catalog_refresh_cost if operation == "refresh" else config.tool_call_cost
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

        async def workflow(begin: Callable[[], Awaitable[None]]) -> T:
            requests = McpHttpRequests(owner, begin, source_guard)
            with request_runner_scope(requests):
                result = await invoke()
                if requests.denied is not None:
                    raise requests.denied
                if isinstance(result, McpCallPayload) and result.is_error:
                    raise BudgetDenied("mcp_remote_tool_error")
                return result

        return await operate_with_run(
            self._database,
            OperationPolicy(
                snapshot.version, tuple(snapshot.config.run_budget.model_dump().items()), quote
            ),
            user_id=owner,
            privacy_level=PrivacyLevel.L1,
            entry=f"mcp.{operation}",
            invoke=workflow,
            evidence=lambda _: {"mcp_transport_response": "true"},
            source_guard=source_guard,
            source_commit_guard=source_guard,
            cost_endpoint=f"mcp.{config.server_id}.{operation}",
            budget_source=lambda: self._store.current.config.run_budget,
            cooperative=True,
            defer_watch=closing,
            missing_quote_reason="admin_cost_estimate_unavailable",
        )
