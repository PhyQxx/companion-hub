"""L3 must not escape an inherited durable quota through the legacy decoder."""

from pathlib import Path

import pytest
from sqlalchemy import select
from test_voice_parent_budget import parent_budget
from test_voice_turn_delivery import fixture
from test_voice_websocket import FakeStreamingRecognizer

from app.config.models import RunBudgetConfig
from app.db import ModelCostRecord, TaskRunRecord
from app.harness.budget import BudgetDenied, budget_scope, tool_budget_scope
from app.runs.budget import RunModelBudget
from app.schemas import PrivacyLevel


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("facet", ["model", "tool"])
@pytest.mark.parametrize("mode", ["capture", "proactive"])
async def test_ephemeral_audio_cannot_bypass_inherited_money_cap(
    backend: str, facet: str, mode: str, tmp_path: Path
) -> None:
    recognizer = FakeStreamingRecognizer()
    storage, manager, session, service, synthesizer, _, _ = await fixture(
        backend, tmp_path, recognizer, enabled=False
    )
    try:
        original = await parent_budget(storage, session)
        config = RunBudgetConfig(cost_currency="CNY", max_daily_cost=0.001)
        async with storage.database.sessions.begin() as sql:
            root = await sql.get_one(TaskRunRecord, original.run_id)
            root.budget = config.model_dump(mode="json")
        parent = RunModelBudget(
            storage.database, run_id=original.run_id, user_id=original.owner_id, config=config
        )
        session.privacy_level = PrivacyLevel.L3
        scope = budget_scope(parent) if facet == "model" else tool_budget_scope(parent.tool_budget)
        with scope, pytest.raises(BudgetDenied, match="ephemeral_operation_run_forbidden"):
            if mode == "capture":
                await manager._start_streamer(session, b"\x01\x00" * 16)
            else:
                await manager._send_proactive(session, "synthetic speech", PrivacyLevel.L1)
        assert recognizer.fed_frames == synthesizer.calls == 0
        async with storage.database.sessions() as sql:
            assert list(await sql.scalars(select(TaskRunRecord.id))) == [original.run_id]
            assert not list(await sql.scalars(select(ModelCostRecord)))
    finally:
        await manager.disconnect(session)
        await service.drain_background_work()
        await storage.close()
