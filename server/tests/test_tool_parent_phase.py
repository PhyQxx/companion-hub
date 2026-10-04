"""A tool facade preserves its model origin's active-maintenance permission."""

from contextlib import ExitStack
from pathlib import Path

import pytest
from test_voice_parent_budget import discard, parent_budget
from test_voice_turn_delivery import fixture
from test_voice_websocket import FakeRecognizer

from app.harness.budget import BudgetDenied, budget_scope, tool_budget_scope
from app.runs.budget import RunModelBudget
from app.runs.resources import RunToolBudget


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("facet", ["model", "tool", "both_stricter"])
@pytest.mark.parametrize("allow_active", [False, True])
async def test_tool_facade_cannot_enable_active_parent_model_maintenance(
    backend: str,
    facet: str,
    allow_active: bool,
    tmp_path: Path,
) -> None:
    storage, manager, session, service, _, provider, _ = await fixture(
        backend,
        tmp_path,
        FakeRecognizer("unused"),
        tts=False,
    )
    context = None
    try:
        assert session.conversation_id is not None and manager._voice_turn_delivery is not None
        original = await parent_budget(storage, session, maximum=2)
        parent = RunModelBudget(
            storage.database,
            run_id=original.run_id,
            user_id=original.owner_id,
            config=original.budget_config,
            phase="maintenance",
            allow_active_parent=allow_active,
            delivery_deadline=original.delivery_deadline,
        )
        expected_active = allow_active and facet != "both_stricter"
        with ExitStack() as scopes:
            if facet == "model":
                scopes.enter_context(budget_scope(parent))
            elif facet == "tool":
                scopes.enter_context(tool_budget_scope(parent.tool_budget))
            else:
                scopes.enter_context(budget_scope(parent))
                scopes.enter_context(
                    tool_budget_scope(
                        RunToolBudget(
                            storage.database,
                            run_id=parent.run_id,
                            user_id=parent.owner_id,
                            config=parent.budget_config,
                            maintenance=True,
                            deadline=parent.delivery_deadline,
                            allow_active_model_parent=False,
                        )
                    )
                )
            context = await manager._voice_turn_delivery.start(manager._source_claim(session))
        for attempt in range(2 if expected_active else 1):
            pending = await service.start_turn(
                session.conversation_id,
                user_id=original.owner_id,
                text="synthetic utterance",
                privacy_level=session.privacy_level,
                parent_run_id=context.run_id,
                parent_budget_scope=context.quota_scope,
            )
            if expected_active and attempt == 0:
                await service.run_stream(pending, discard)
            else:
                with pytest.raises(BudgetDenied):
                    await service.run_stream(pending, discard)
        assert len(provider.requests) == (1 if expected_active else 0)
        assert context.quota_scope is not None
        assert context.quota_scope["allow_active_parent"] is expected_active
    finally:
        if context is not None:
            await context.finish("cancelled", "synthetic_cleanup")
        await service.drain_background_work()
        await storage.close()
