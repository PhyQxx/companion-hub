"""A voice trace child keeps the original quota, even after a config change."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from test_voice_turn_delivery import fixture
from test_voice_websocket import FakeRecognizer

from app.api.voice_ws import VoiceSession
from app.config.models import RunBudgetConfig
from app.db import (
    ModelCostRecord,
    ModelReservationRecord,
    TaskRunRecord,
)
from app.harness.budget import BudgetDenied, budget_scope
from app.ids import uuid7
from app.runs.budget import RunModelBudget
from app.runs.resources import resource_usage
from scripts.benchmark_storage import FixtureStorage


async def parent_budget(
    storage: FixtureStorage,
    session: VoiceSession,
    *,
    maximum: int = 8,
    currency: str | None = "CNY",
    port_enabled: bool = True,
) -> RunModelBudget:
    now = datetime.now(UTC)
    identifier = uuid7()
    config = RunBudgetConfig(max_llm_attempts=8, cost_currency=currency)
    async with storage.database.sessions.begin() as sql:
        sql.add(
            TaskRunRecord(
                id=identifier,
                user_id=session.principal.user_id,
                conversation_id=session.conversation_id,
                status="running",
                privacy_level="L1",
                contract={"entry": "synthetic.parent", "required_work": []},
                budget=config.model_dump(mode="json"),
                deadline=now + timedelta(minutes=2),
                created_at=now,
                updated_at=now,
            )
        )
    return RunModelBudget(
        storage.database,
        run_id=identifier,
        user_id=session.principal.user_id,
        config=config.model_copy(update={"max_llm_attempts": maximum, "enabled": port_enabled}),
    )


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("currency", [None, "CNY"])
@pytest.mark.parametrize("port_enabled", [False, True])
async def test_voice_children_charge_original_root(
    backend: str, enabled: bool, currency: str | None, port_enabled: bool, tmp_path: Path
) -> None:
    recognizer = FakeRecognizer("synthetic utterance")
    storage, manager, session, service, _, provider, _ = await fixture(
        backend, tmp_path, recognizer, enabled=enabled
    )
    try:
        parent = await parent_budget(storage, session, currency=currency, port_enabled=port_enabled)
        _, chain = await manager._voice_source.resolve()
        with budget_scope(parent):
            await manager._run_utterance(session, b"\x01\x00" * 16000, recognizer, chain)
        async with storage.database.sessions() as sql:
            runs = list(await sql.scalars(select(TaskRunRecord)))
            root = next(row for row in runs if row.id == parent.run_id)
            voice = next(row for row in runs if row.contract.get("entry") == "voice.utterance")
            chat = next(row for row in runs if row.contract.get("kind") == "chat.reply")
            assert voice.status == chat.status == "succeeded"
            assert voice.parent_run_id == root.id and chat.parent_run_id == voice.id
            assert root.llm_attempts == 1 and resource_usage(root)["tool_attempts"] == 2
            assert voice.llm_attempts == chat.llm_attempts == 0
            assert resource_usage(voice).get("tool_attempts", 0) == 0
            reservations = list(await sql.scalars(select(ModelReservationRecord)))
            assert len(reservations) == 1
            assert all(row.run_id == root.id for row in reservations)
            fees = list(
                await sql.scalars(select(ModelCostRecord).where(ModelCostRecord.unit == "request"))
            )
            assert len(fees) == 2 and sum(row.charged_micros or 0 for row in fees) == 5000
            assert all(row.user_id == root.user_id and row.state == "unknown" for row in fees)
        assert len(provider.requests) == 1
    finally:
        await service.drain_background_work()
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_voice_chat_preserves_stricter_in_memory_parent_limit(
    backend: str, tmp_path: Path
) -> None:
    recognizer = FakeRecognizer("synthetic utterance")
    storage, manager, session, service, _, provider, _ = await fixture(
        backend, tmp_path, recognizer, tts=False
    )
    try:
        parent = await parent_budget(storage, session, maximum=2)
        assert session.conversation_id is not None
        assert manager._voice_turn_delivery is not None
        with budget_scope(parent):
            context = await manager._voice_turn_delivery.start(manager._source_claim(session))
        try:
            for attempt in range(3):
                pending = await service.start_turn(
                    session.conversation_id,
                    user_id=session.principal.user_id,
                    text="synthetic utterance",
                    privacy_level=session.privacy_level,
                    parent_run_id=context.run_id,
                    parent_budget_scope=context.quota_scope,
                )
                budget = service._model_budget(pending)
                assert budget is not None and budget.run_id == parent.run_id
                if attempt == 2:
                    with pytest.raises(BudgetDenied, match="run_budget_exhausted"):
                        await service.run_stream(pending, discard)
                else:
                    await service.run_stream(pending, discard)
            assert len(provider.requests) == 2
        finally:
            await context.finish("cancelled", "synthetic_cleanup")
    finally:
        await service.drain_background_work()
        await storage.close()


async def discard(delta: str) -> None:
    pass


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_original_parent_cancel_stops_attached_chat(backend: str, tmp_path: Path) -> None:
    recognizer = FakeRecognizer("synthetic utterance")
    storage, manager, session, service, _, provider, _ = await fixture(
        backend, tmp_path, recognizer, tts=False
    )
    try:
        parent = await parent_budget(storage, session)
        assert session.conversation_id is not None
        assert manager._voice_turn_delivery is not None
        with budget_scope(parent):
            context = await manager._voice_turn_delivery.start(manager._source_claim(session))
        try:
            pending = await service.start_turn(
                session.conversation_id,
                user_id=session.principal.user_id,
                text="synthetic utterance",
                privacy_level=session.privacy_level,
                parent_run_id=context.run_id,
                parent_budget_scope=context.quota_scope,
            )
            async with storage.database.sessions.begin() as sql:
                await sql.execute(
                    update(TaskRunRecord)
                    .where(TaskRunRecord.id == parent.run_id)
                    .values(status="cancelled")
                )
            with pytest.raises(BudgetDenied):
                await service.run_stream(pending, discard)
            assert not provider.requests
        finally:
            await context.finish("cancelled", "synthetic_cleanup")
    finally:
        await service.drain_background_work()
        await storage.close()
