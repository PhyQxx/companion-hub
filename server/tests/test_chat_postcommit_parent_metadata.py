"""Missing parent metadata must not turn attached maintenance into a new scope."""

from pathlib import Path

import pytest
from sqlalchemy import update
from test_voice_parent_budget import discard, parent_budget
from test_voice_turn_delivery import fixture
from test_voice_websocket import FakeRecognizer

from app.chat import PendingTurn
from app.chat.postcommit import PostcommitSourceGone
from app.db import AuthSessionRecord, TaskRunRecord
from app.harness.budget import BudgetDenied, budget_scope


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize(
    "change",
    [
        "metadata_missing",
        "metadata_null",
        "metadata_empty",
        "metadata_wrong",
        "foreign_key_missing",
        "scope_missing",
        "scope_invalid",
        "scope_only",
        "revoked_actor_and_missing_metadata",
    ],
)
async def test_changed_parent_metadata_denies_before_maintenance_dispatch(
    backend: str,
    change: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage, manager, session, service, _, _, _ = await fixture(
        backend,
        tmp_path,
        FakeRecognizer("unused"),
        tts=False,
    )
    context = None
    dispatched = False

    async def inspect(pending: PendingTurn, **kwargs: object) -> None:
        nonlocal dispatched
        dispatched = True

    monkeypatch.setattr(service, "_consolidate_memory", inspect)
    try:
        assert session.conversation_id is not None and manager._voice_turn_delivery is not None
        parent = await parent_budget(storage, session)
        with budget_scope(parent):
            context = await manager._voice_turn_delivery.start(manager._source_claim(session))
        pending = await service.start_turn(
            session.conversation_id,
            user_id=parent.owner_id,
            text="synthetic utterance",
            privacy_level=session.privacy_level,
            parent_run_id=context.run_id,
            parent_budget_scope=context.quota_scope,
        )
        turn = await service.run_stream(pending, discard)
        await context.finish("succeeded", "voice_reply_sent")
        async with storage.database.sessions.begin() as sql:
            original = await sql.get_one(TaskRunRecord, parent.run_id)
            original.status = "succeeded"
            child = await sql.get_one(TaskRunRecord, pending.turn_id)
            contract = dict(child.contract)
            if change in {"metadata_missing", "scope_only", "revoked_actor_and_missing_metadata"}:
                contract.pop("budget_parent_id")
            elif change == "metadata_null":
                contract["budget_parent_id"] = None
            elif change == "metadata_empty":
                contract["budget_parent_id"] = ""
            elif change == "metadata_wrong":
                contract["budget_parent_id"] = str(parent.run_id)
            elif change == "scope_missing":
                contract.pop("quota_scope")
            elif change == "scope_invalid":
                contract["quota_scope"] = {"run_id": str(parent.run_id)}
            if change in {"foreign_key_missing", "scope_only"}:
                child.parent_run_id = None
            child.contract = contract
            if change == "revoked_actor_and_missing_metadata":
                from datetime import UTC, datetime

                await sql.execute(update(AuthSessionRecord).values(revoked_at=datetime.now(UTC)))
        with pytest.raises((PostcommitSourceGone, BudgetDenied)):
            await service._run_postcommit(
                "chat.memory",
                {"assistant_message_id": str(turn.assistant_message.id)},
                str(parent.owner_id),
            )
        assert not dispatched
    finally:
        if context is not None:
            await context.finish("cancelled", "synthetic_cleanup")
        await service.drain_background_work()
        await storage.close()
