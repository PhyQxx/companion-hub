"""An explicit tool facet cannot be replaced by a fresh voice/media quota."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from test_voice_parent_budget import parent_budget
from test_voice_turn_delivery import fixture
from test_voice_websocket import FakeRecognizer

from app.db import ModelCostRecord, TaskRunRecord
from app.harness.budget import (
    BudgetDenied,
    budget_scope,
    current_budget,
    current_tool_budget,
    tool_budget_scope,
)
from app.harness.operations import OperationPolicy
from app.harness.unit_costs import UnitCostQuote, UnitPricing
from app.runs.budget import RunModelBudget
from app.runs.operation import operate_with_run
from app.runs.resources import RunToolBudget, resource_usage
from app.schemas import PrivacyLevel


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("mode", ["voice", "speech", "operation"])
@pytest.mark.parametrize(
    "case",
    [
        "only_tool",
        "money_cap",
        "different_roots",
        "same_root_stricter",
        "tool_expired",
        "completed_tool",
        "currency_mismatch",
        "phase_mismatch",
    ],
)
async def test_explicit_tool_facet_retains_root_and_limits(
    backend: str,
    mode: str,
    case: str,
    tmp_path: Path,
) -> None:
    recognizer = FakeRecognizer("synthetic utterance")
    storage, manager, session, service, synthesizer, provider, store = await fixture(
        backend,
        tmp_path,
        recognizer,
        enabled=False,
    )
    called = False
    try:
        parent = await parent_budget(storage, session)
        config = parent.budget_config.model_copy(
            update={
                "max_tool_attempts": 2,
                **({"max_daily_cost": 0.001} if case == "money_cap" else {}),
                **({"max_daily_cost": 0.01} if case == "currency_mismatch" else {}),
            }
        )
        async with storage.database.sessions.begin() as sql:
            root = await sql.get_one(TaskRunRecord, parent.run_id)
            root.budget = config.model_dump(mode="json")
            if case == "completed_tool":
                root.status = "succeeded"
                root.deadline = datetime.now(UTC) - timedelta(seconds=1)
        parent = RunModelBudget(
            storage.database, run_id=parent.run_id, user_id=parent.owner_id, config=config
        )
        model = None
        tool = parent.tool_budget
        if case == "different_roots":
            model = parent
            other = await parent_budget(storage, session)
            tool = other.tool_budget

        if case == "same_root_stricter":
            model = parent
            tool = RunToolBudget(
                storage.database,
                run_id=parent.run_id,
                user_id=parent.owner_id,
                config=config.model_copy(update={"max_tool_attempts": 1}),
                deadline=parent.delivery_deadline,
            )
            permit = await tool.reserve_tool(
                tool_name="synthetic.preexisting", user_id=parent.owner_id
            )
            await tool.settle_tool(permit.call_id, reported_ok=True)
        elif case in {"tool_expired", "completed_tool", "phase_mismatch"}:
            tool = RunToolBudget(
                storage.database,
                run_id=parent.run_id,
                user_id=parent.owner_id,
                config=config,
                maintenance=case != "tool_expired",
                deadline=datetime.now(UTC)
                + timedelta(seconds=-1 if case == "tool_expired" else 30),
            )
            if case == "phase_mismatch":
                model = parent
        elif case == "currency_mismatch":
            model = parent
            tool = RunToolBudget(
                storage.database,
                run_id=parent.run_id,
                user_id=parent.owner_id,
                config=config.model_copy(update={"cost_currency": "USD"}),
                deadline=parent.delivery_deadline,
            )

        async def invoke(start: Callable[[], Awaitable[None]]) -> bool:
            nonlocal called
            budget = current_budget()
            if case in {"only_tool", "completed_tool"}:
                assert isinstance(budget, RunModelBudget) and budget.run_id == parent.run_id
            current = current_tool_budget()
            assert current is not None
            permit = await current.reserve_tool(tool_name="synthetic.sdk", user_id=parent.owner_id)
            await start()
            called = True
            await current.settle_tool(permit.call_id, reported_ok=True)
            return True

        async def guard() -> None:
            await manager._validate_source(session, manager._source_claim(session))

        error: BudgetDenied | None = None
        try:
            with budget_scope(model), tool_budget_scope(tool):
                if mode == "voice":
                    _, chain = await manager._voice_source.resolve()
                    await manager._run_utterance(session, b"\x01\x00" * 16000, recognizer, chain)
                elif mode == "speech":
                    await manager._send_proactive(session, "synthetic speech", PrivacyLevel.L1)
                else:
                    cfg = store.current
                    await operate_with_run(
                        storage.database,
                        OperationPolicy(
                            cfg.version,
                            tuple(cfg.config.run_budget.model_dump().items()),
                            UnitCostQuote(
                                pricing=UnitPricing(
                                    unit="request", currency="CNY", rate_per_unit=".002"
                                ),
                                maximum_quantity=1,
                            ),
                        ),
                        user_id=parent.owner_id,
                        privacy_level=PrivacyLevel.L1,
                        entry="synthetic.sdk",
                        invoke=invoke,
                        evidence=lambda _: {},
                        source_guard=guard,
                        cost_endpoint="synthetic.sdk",
                    )
        except BudgetDenied as denied:
            error = denied
        async with storage.database.sessions() as sql:
            root = await sql.get_one(TaskRunRecord, parent.run_id)
            runs = list(await sql.scalars(select(TaskRunRecord)))
            costs = list(await sql.scalars(select(ModelCostRecord)))
        if case not in {"only_tool", "completed_tool"}:
            if mode != "voice":
                assert error is not None
            assert recognizer.calls == synthesizer.calls == len(provider.requests) == 0
            assert not called and all(row.charged_micros == 0 for row in costs)
            if case == "same_root_stricter":
                assert resource_usage(root)["tool_attempts"] == 1
        else:
            assert error is None
            assert root.status == ("succeeded" if case == "completed_tool" else "running")
            assert root.llm_attempts == (1 if mode == "voice" else 0)
            assert resource_usage(root)["tool_attempts"] == (2 if mode == "voice" else 1)
            children = [row for row in runs if row.id != parent.run_id]
            assert children and all(row.status == "succeeded" for row in children)
            if mode == "voice":
                voice = next(
                    row for row in children if row.contract.get("entry") == "voice.utterance"
                )
                assert voice.parent_run_id == parent.run_id
                assert (
                    next(
                        row for row in children if row.contract.get("kind") == "chat.reply"
                    ).parent_run_id
                    == voice.id
                )
            else:
                assert any(row.parent_run_id == parent.run_id for row in children)
            assert all(row.state == "unknown" for row in costs)
    finally:
        await manager.disconnect(session)
        await service.drain_background_work()
        await storage.close()
