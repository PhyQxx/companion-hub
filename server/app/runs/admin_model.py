"""Meter actual model inference using the containing admin request quote."""

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

from app.harness.budget import BudgetDenied, current_budget
from app.harness.joined_read import join_on_cancel
from app.harness.operations import current_operation_cost
from app.harness.window import fit_window
from app.llm.contracts import CompletionResult, ModelEndpoint, ModelUsage
from app.llm.probe import ProbeInference, probe_request, validate_probe

from .admin_operation import admin_tool_request
from .budget import RunModelBudget


async def admin_model_probe(
    *,
    owner: UUID,
    endpoint: ModelEndpoint,
    provider_factory: Callable[[], ProbeInference],
    mark_started: Callable[[], Awaitable[None]],
    source_guard: Callable[[], Awaitable[None]],
) -> None:
    budget = current_budget()
    cost = current_operation_cost()
    if cost is None or cost.user_id != owner:
        raise BudgetDenied("unit_cost_reservation_inactive")
    if budget is not None and not isinstance(budget, RunModelBudget):
        raise BudgetDenied("budget_parent_port_invalid")
    # Keep the probe small even when the endpoint's normal output limit is large.
    effective = endpoint.model_copy(
        update={"max_tokens": min(endpoint.max_tokens or 128, 1024), "max_retries": 0}
    )
    request = probe_request(effective)
    fitted = await join_on_cancel(
        asyncio.to_thread(fit_window, request, effective), name="admin-probe-window"
    )
    tokens = sum(
        int(fitted.manifest[key])
        for key in (
            "estimated_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "protocol_reserve_tokens",
        )
    )
    permit = (
        await budget.reserve_unit_attempt(cost=cost, tokens=tokens, final=not request.tools)
        if budget
        else None
    )
    result: CompletionResult | None = None
    started = False

    async def invoke() -> None:
        nonlocal started, result
        started = True
        # Construct the SDK only after the model, tool, fee and source gates.
        provider = provider_factory()
        async with asyncio.timeout(
            min(
                effective.timeout_ms / 1000,
                permit.remaining_seconds if permit else effective.timeout_ms / 1000,
            )
        ):
            result = await provider.complete(fitted.request)
        validate_probe(result, effective)

    try:
        await admin_tool_request(
            owner=owner,
            tool_name="admin.models.inference",
            invoke=invoke,
            source_guard=source_guard,
            before_start=mark_started,
        )
    finally:
        if budget is not None and permit is not None:
            usage = result.usage if result is not None else None
            if not started:
                usage = ModelUsage(
                    input_tokens=0, output_tokens=0, total_tokens=0, usage_known=True
                )
            await join_on_cancel(
                budget.settle_unit_attempt(permit.call_id, usage), name="admin-probe-model-settle"
            )
