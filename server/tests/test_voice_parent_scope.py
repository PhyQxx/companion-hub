"""Nested voice chats fail closed on quota lineage and authority changes."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select, update
from test_voice_parent_budget import discard, parent_budget
from test_voice_turn_delivery import fixture
from test_voice_websocket import FakeRecognizer

from app.chat import PendingTurn
from app.config.models import RunBudgetConfig
from app.db import AuthSessionRecord, ConversationRecord, MessageRecord, TaskRunRecord
from app.harness.budget import BudgetDenied, budget_scope, current_budget
from app.harness.time import utc
from app.ids import uuid7
from app.llm.contracts import ModelPricing
from app.runs.budget import RunModelBudget


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize(
    "change",
    [
        "quota_deleted",
        "quota_privacy",
        "quota_expired",
        "quota_overflow",
        "quota_disabled",
        "quota_conversation",
        "quota_nested",
        "voice_link",
        "voice_scope",
        "voice_actor",
        "child_link",
        "child_scope",
        "actor_revoked",
        "conversation_archived",
    ],
)
async def test_changed_voice_quota_or_authority_cannot_dispatch_chat(
    backend: str, change: str, tmp_path: Path
) -> None:
    storage, manager, session, service, _, provider, _ = await fixture(
        backend, tmp_path, FakeRecognizer("unused"), tts=False
    )
    try:
        assert session.conversation_id is not None
        parent = await parent_budget(storage, session)
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
                quota = await sql.get_one(TaskRunRecord, parent.run_id)
                voice = await sql.get_one(TaskRunRecord, context.run_id)
                child = await sql.get_one(TaskRunRecord, pending.turn_id)
                if change == "quota_deleted":
                    await sql.delete(quota)
                elif change == "quota_privacy":
                    quota.privacy_level = "L2"
                elif change == "quota_expired":
                    quota.deadline = datetime.now(UTC) - timedelta(seconds=1)
                elif change == "quota_overflow":
                    quota.contract = {**quota.contract, "budget_usage_overflow": True}
                elif change == "quota_disabled":
                    quota.budget = {**(quota.budget or {}), "enabled": False}
                elif change == "quota_conversation":
                    quota.conversation_id = None
                elif change == "quota_nested":
                    quota.parent_run_id = voice.id
                elif change == "voice_link":
                    voice.parent_run_id = None
                elif change == "voice_scope":
                    voice.contract = {**voice.contract, "quota_scope": None}
                elif change == "voice_actor":
                    voice.contract = {**voice.contract, "source_id": str(uuid7())}
                elif change == "child_link":
                    child.parent_run_id = None
                elif change == "child_scope":
                    child.contract = {**child.contract, "quota_scope": None}
                elif change == "actor_revoked":
                    actor = await sql.get_one(AuthSessionRecord, session.principal.session_id)
                    actor.revoked_at = datetime.now(UTC)
                else:
                    conversation = await sql.get_one(ConversationRecord, session.conversation_id)
                    conversation.status = "archived"
            with pytest.raises(BudgetDenied):
                await service.run_stream(pending, discard)
            assert not provider.requests
            async with storage.database.sessions() as sql:
                assert not list(
                    await sql.scalars(
                        select(MessageRecord.id).where(MessageRecord.role == "assistant")
                    )
                )
        finally:
            await context.finish("cancelled", "synthetic_cleanup")
    finally:
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("state", ["running", "succeeded", "cancelled"])
async def test_voice_postcommit_restores_original_scope(
    backend: str, state: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, manager, session, service, _, _, _ = await fixture(
        backend, tmp_path, FakeRecognizer("unused"), tts=False
    )
    seen: list[UUID] = []
    parent = await parent_budget(storage, session, maximum=2)

    async def inspect(pending: PendingTurn, **kwargs: object) -> None:
        budget = current_budget()
        assert isinstance(budget, RunModelBudget)
        assert budget.run_id == parent.run_id and budget.budget_config.max_llm_attempts == 2
        assert budget.delivery_deadline == parent.delivery_deadline
        seen.append(budget.run_id)
        permit = await budget.reserve(
            endpoint="synthetic_maintenance",
            tokens=1,
            final=False,
            pricing=ModelPricing(currency="CNY", input_rate=0, output_rate=0),
        )
        await budget.settle(permit.call_id, None)

    monkeypatch.setattr(service, "_consolidate_memory", inspect)
    context = None
    try:
        assert session.conversation_id is not None and manager._voice_turn_delivery is not None
        with budget_scope(parent):
            context = await manager._voice_turn_delivery.start(manager._source_claim(session))
        pending = await service.start_turn(
            session.conversation_id,
            user_id=session.principal.user_id,
            text="synthetic utterance",
            privacy_level=session.privacy_level,
            parent_run_id=context.run_id,
            parent_budget_scope=context.quota_scope,
        )
        turn = await service.run_stream(pending, discard)
        await context.finish("succeeded", "voice_reply_sent")
        async with storage.database.sessions.begin() as sql:
            await sql.execute(
                update(TaskRunRecord).where(TaskRunRecord.id == parent.run_id).values(status=state)
            )
        call = service._run_postcommit(
            "chat.memory",
            {"assistant_message_id": str(turn.assistant_message.id)},
            str(parent.owner_id),
        )
        if state == "succeeded":
            await call
            assert seen == [parent.run_id]
        elif state == "running":
            with pytest.raises(BudgetDenied, match="run_budget_exhausted"):
                await call
            assert seen == [parent.run_id]
        else:
            with pytest.raises(BudgetDenied):
                await call
            assert not seen
    finally:
        if context is not None:
            await context.finish("cancelled", "synthetic_cleanup")
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("allow_active", [False, True])
async def test_voice_chat_keeps_original_maintenance_phase(
    backend: str, allow_active: bool, tmp_path: Path
) -> None:
    storage, manager, session, service, _, provider, _ = await fixture(
        backend, tmp_path, FakeRecognizer("unused"), tts=False
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
        with budget_scope(parent):
            context = await manager._voice_turn_delivery.start(manager._source_claim(session))
        for attempt in range(2 if allow_active else 1):
            pending = await service.start_turn(
                session.conversation_id,
                user_id=session.principal.user_id,
                text="synthetic utterance",
                privacy_level=session.privacy_level,
                parent_run_id=context.run_id,
                parent_budget_scope=context.quota_scope,
            )
            if allow_active and attempt == 0:
                await service.run_stream(pending, discard)
            else:
                with pytest.raises(BudgetDenied):
                    await service.run_stream(pending, discard)
        # Maintenance cannot use the live parent's reserved final-answer slot.
        assert len(provider.requests) == (1 if allow_active else 0)
    finally:
        if context is not None:
            await context.finish("cancelled", "synthetic_cleanup")
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_voice_child_does_not_renew_original_memory_deadline(
    backend: str, tmp_path: Path
) -> None:
    storage, manager, session, service, _, provider, _ = await fixture(
        backend, tmp_path, FakeRecognizer("unused"), tts=False
    )
    context = None
    try:
        assert session.conversation_id is not None and manager._voice_turn_delivery is not None
        original = await parent_budget(storage, session)
        deadline = datetime.now(UTC) + timedelta(seconds=2)
        parent = RunModelBudget(
            storage.database,
            run_id=original.run_id,
            user_id=original.owner_id,
            config=original.budget_config,
            delivery_deadline=deadline,
        )
        with budget_scope(parent):
            context = await manager._voice_turn_delivery.start(manager._source_claim(session))
        pending = await service.start_turn(
            session.conversation_id,
            user_id=session.principal.user_id,
            text="synthetic utterance",
            privacy_level=session.privacy_level,
            parent_run_id=context.run_id,
            parent_budget_scope=context.quota_scope,
        )
        assert context.deadline == deadline
        async with storage.database.sessions() as sql:
            child = await sql.get_one(TaskRunRecord, pending.turn_id)
            assert child.deadline is not None and utc(child.deadline) == deadline
        await asyncio.sleep(max(0.0, (deadline - datetime.now(UTC)).total_seconds()) + 0.02)
        with pytest.raises(BudgetDenied):
            await service.run_stream(pending, discard)
        assert not provider.requests
    finally:
        if context is not None:
            await context.finish("cancelled", "synthetic_cleanup")
        await service.drain_background_work()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_different_parent_and_current_money_currencies_are_not_combined(
    backend: str, tmp_path: Path
) -> None:
    storage, manager, session, service, _, provider, _ = await fixture(
        backend, tmp_path, FakeRecognizer("unused"), tts=False
    )
    try:
        original = await parent_budget(storage, session)
        parent = RunModelBudget(
            storage.database,
            run_id=original.run_id,
            user_id=original.owner_id,
            config=RunBudgetConfig(cost_currency="USD", max_daily_cost=1),
        )
        assert manager._voice_turn_delivery is not None
        with budget_scope(parent), pytest.raises(BudgetDenied, match="budget_currency_mismatch"):
            await manager._voice_turn_delivery.start(manager._source_claim(session))
        assert not provider.requests
        async with storage.database.sessions() as sql:
            assert list(await sql.scalars(select(TaskRunRecord.id))) == [original.run_id]
    finally:
        await service.drain_background_work()
        await storage.close()
