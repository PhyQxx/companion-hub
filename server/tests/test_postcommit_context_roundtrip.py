"""Persisted context ancestry must survive the JSON boundary before extraction."""

from pathlib import Path

import pytest
from sqlalchemy import delete
from test_chat_parent_budget import discard_delta, fixture, start
from test_memory import candidate

from app.chat import PendingTurn
from app.chat.postcommit import PostcommitSourceGone
from app.context.repository import attach_memory_lineage, version_stamp
from app.db import MemorySourceRecord, MessageRecord, TaskRunRecord
from app.harness.context import ContextReference
from app.jobs.maintenance import MaintenanceFailure
from app.memory import MemoryStore


@pytest.mark.parametrize("state", ["valid", "malformed", "revoked"])
async def test_postcommit_reads_persisted_ancestry(
    state: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, service, owner, root, conversation, _ = await fixture("sqlite", tmp_path)
    dispatched = False

    async def inspect(pending: PendingTurn, **kwargs: object) -> None:
        nonlocal dispatched
        dispatched = True

    monkeypatch.setattr(service, "_consolidate_memory", inspect)
    try:
        memory = await MemoryStore(storage.database).add(
            candidate("synthetic preference"), user_id=owner
        )
        pending = await start(service, owner, root, conversation)
        turn = await service.run_stream(pending, discard_delta)
        async with storage.database.sessions.begin() as session:
            references = await attach_memory_lineage(
                session,
                (
                    ContextReference(
                        kind="memory",
                        source_id=str(memory.id),
                        owner_id=str(owner),
                        privacy_level="L1",
                        version=version_stamp(memory.updated_at),
                    ),
                ),
                owner_id=owner,
            )
            manifest = references[0].manifest()
            assert manifest["lineage"]
            if state == "malformed":
                manifest["lineage"] = [["manual", {"secret": "must not appear in diagnostics"}]]
            row = await session.get_one(MessageRecord, turn.assistant_message.id)
            row.decision_meta = {
                **(row.decision_meta or {}),
                "context_sources": [{"references": [manifest]}],
            }
            (await session.get_one(TaskRunRecord, root)).status = "succeeded"
            if state == "revoked":
                await session.execute(
                    delete(MemorySourceRecord).where(MemorySourceRecord.memory_id == memory.id)
                )
        async with storage.database.sessions() as session:
            row = await session.get_one(MessageRecord, turn.assistant_message.id)
            assert row.decision_meta is not None
            assert isinstance(
                row.decision_meta["context_sources"][0]["references"][0]["lineage"][0], list
            )
        call = service._run_postcommit(
            "chat.memory", {"assistant_message_id": str(turn.assistant_message.id)}, str(owner)
        )
        if state == "valid":
            await call
            assert dispatched
        elif state == "malformed":
            with pytest.raises(MaintenanceFailure) as caught:
                await call
            assert caught.value.reason_code == "postcommit_context_metadata_invalid"
            assert caught.value.detail == {
                "stage": "context_deserialization",
                "error_type": "ValueError",
            }
            assert not dispatched
        else:
            with pytest.raises(PostcommitSourceGone):
                await call
            assert not dispatched
    finally:
        await storage.close()


@pytest.mark.parametrize(
    "lineage", [None, "ab", ["ab"], [["a"]], [["a", "b", "c"]], [["a", []]], [["", "b"]]]
)
def test_invalid_ancestry_is_not_silently_discarded(lineage: object) -> None:
    with pytest.raises(ValueError, match="context_reference_lineage_invalid"):
        ContextReference(
            kind="memory",
            source_id="1",
            owner_id="owner",
            privacy_level="L1",
            lineage=lineage,  # type: ignore[arg-type]
        )
