"""Restore an actual older database image while preserving newer deletion intent."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from test_chat import _summary_service, create_user

from app.db import (
    ActionPlanRecord,
    ConversationRecord,
    DeletionLedgerRecord,
    JobRecord,
    MemoryRecord,
    SkillDraftRecord,
    TaskRunRecord,
    TimelineEventRecord,
    WorkflowDraftRecord,
    create_database,
)
from app.ids import uuid7
from app.jobs import JobEngine
from app.memory import MemoryCandidate, MemorySourceRef, MemoryStore
from app.memory.replay import import_deletion_journal, replay_deletions
from app.privacy.deletion_journal import DeletionIntent, DeletionJournal
from app.schemas import PrivacyLevel
from app.skills.generator import SkillProposal
from app.skills.models import SkillDocument
from app.skills.store import SkillStore
from app.timeline import TimelineActor, TimelineStore
from app.workflows.drafts import WorkflowDraftStore
from app.workflows.models import WorkflowStep


async def test_old_backup_replay_purges_runtime_and_candidates(tmp_path: Path) -> None:
    service, database, _ = await _summary_service(tmp_path)
    journal = DeletionJournal(tmp_path / "separate-volume" / "journal.jsonl")
    memory = MemoryStore(database, deletion_journal=journal)
    service._memory_store = memory
    source_user = await create_user(database)
    conversation = await service.create_conversation(user_id=source_user.id, title="source")
    other = await service.create_conversation(user_id=source_user.id, title="keep")
    pending = await service.start_turn(
        conversation.id,
        user_id=source_user.id,
        text="restored-private-content",
        privacy_level=PrivacyLevel.L2,
    )
    await memory.add(
        MemoryCandidate(
            type="preference",
            content="restored-private-content",
            privacy_level="L2",
            sources=[
                MemorySourceRef(source_kind="message", source_id=str(pending.user_message.id))
            ],
        ),
        user_id=source_user.id,
    )
    await SkillStore(database).save_draft(
        SkillProposal(
            document=SkillDocument(
                name="restore-test", description="private", instructions="private"
            ),
            warnings=[],
            evidence=[],
        ),
        system_name="test",
        source="chat",
        turn_id=str(pending.turn_id),
    )
    now = datetime.now(UTC)
    plan_id = uuid7()
    child_run_id = uuid7()
    async with database.sessions.begin() as session:
        session.add(
            TaskRunRecord(
                id=child_run_id,
                user_id=source_user.id,
                parent_run_id=pending.turn_id,
                status="running",
                privacy_level="L2",
                contract={},
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            ActionPlanRecord(
                id=plan_id,
                user_id=source_user.id,
                task_run_id=child_run_id,
                status="completed",
                idempotency_key="restore",
                request_hash="test",
                expires_at=now + timedelta(minutes=10),
            )
        )
    from test_workflow_drafts import _registry

    from app.cognition import ActionInvocation, ActionPlanService

    pending_plan = await ActionPlanService(database, _registry()).create_plan(
        user_id=source_user.id,
        title="pending private action",
        invocations=[ActionInvocation(action_id="test.read_state", arguments={"target": "test"})],
        source_turn_id=pending.turn_id,
    )
    await WorkflowDraftStore(database).create_draft(
        user_id=source_user.id,
        plan_id=plan_id,
        name="private",
        steps=[WorkflowStep(action_id="read")],
    )
    job = await JobEngine(database).submit(
        "deleg.test",
        {"user_id": str(source_user.id), "topic": "private"},
        owner=str(source_user.id),
        source_turn_id=child_run_id,
    )
    await TimelineStore(database).index_message(
        message_id=pending.user_message.id,
        user_id=source_user.id,
        conversation_id=conversation.id,
        actor=TimelineActor.USER,
        text=pending.user_message.content,
        privacy_level=PrivacyLevel.L2,
        occurred_at=now,
    )
    backup = tmp_path / "old-backup.db"
    with (
        sqlite3.connect(str(database.engine.url.database)) as source,
        sqlite3.connect(backup) as target,
    ):
        source.backup(target)
    receipt = await service.delete_conversation(conversation.id, user_id=source_user.id)
    assert receipt.ledger_id > 0
    assert "private" not in journal.path.read_text()
    await service.drain_background_work()
    await database.close()

    restored = create_database(f"sqlite+aiosqlite:///{backup}")
    try:
        async with restored.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(DeletionLedgerRecord)) == 0
        assert await import_deletion_journal(restored, journal) == 1
        assert await import_deletion_journal(restored, journal) == 0
        dry = await replay_deletions(restored, dry_run=True)
        assert dry.conversations_deleted == 1 and dry.memories_deleted == 1
        applied = await replay_deletions(restored, dry_run=False)
        assert applied.conversations_deleted == 1 and applied.memories_deleted == 1
        async with restored.sessions() as session:
            assert await session.get(ConversationRecord, conversation.id) is None
            assert await session.get(ConversationRecord, other.id) is not None
            for model in (
                MemoryRecord,
                SkillDraftRecord,
                WorkflowDraftRecord,
                TimelineEventRecord,
                TaskRunRecord,
            ):
                assert await session.scalar(select(func.count()).select_from(model)) == 0
            revoked_plan = await session.get(ActionPlanRecord, pending_plan.id)
            assert revoked_plan is not None and revoked_plan.status == "cancelled"
            assert revoked_plan.cancel_requested and revoked_plan.reason_code == "source_deleted"
            stored = await session.get(JobRecord, job.id)
            assert stored is not None and stored.status == "cancelled"
            assert stored.input == {"source_deleted": True}
        again = await replay_deletions(restored, dry_run=False)
        assert again.conversations_deleted == 0 and again.memories_deleted == 0
    finally:
        await restored.close()


async def test_journal_write_failure_rolls_back_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, database, _ = await _summary_service(tmp_path)
    journal = DeletionJournal(tmp_path / "journal.jsonl")
    memory = MemoryStore(database, deletion_journal=journal)
    service._memory_store = memory
    try:
        owner = await create_user(database)
        conversation = await service.create_conversation(user_id=owner.id, title="keep")

        def unavailable(intent: DeletionIntent) -> None:
            raise OSError("simulated unavailable durable journal")

        monkeypatch.setattr(journal, "_append", unavailable)
        with pytest.raises(OSError):
            await service.delete_conversation(conversation.id, user_id=owner.id)
        async with database.sessions() as session:
            assert await session.get(ConversationRecord, conversation.id) is not None
            assert await session.scalar(select(func.count()).select_from(DeletionLedgerRecord)) == 0
    finally:
        await service.drain_background_work()
        await database.close()


async def test_corrupt_journal_never_partially_imports(tmp_path: Path) -> None:
    service, database, _ = await _summary_service(tmp_path)
    journal = DeletionJournal(tmp_path / "journal.jsonl")
    try:
        await journal.append(
            DeletionIntent(
                entity_kind="message",
                entity_id=str(uuid7()),
                deleted_ids=[],
                created_at=datetime.now(UTC),
            )
        )
        with journal.path.open("a") as handle:
            handle.write('{"invalid":')
        with pytest.raises(ValueError, match="invalid_line:2"):
            await import_deletion_journal(database, journal)
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(DeletionLedgerRecord)) == 0
    finally:
        await service.drain_background_work()
        await database.close()


async def test_replay_reads_all_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, database, _ = await _summary_service(tmp_path)
    try:
        monkeypatch.setattr("app.memory.replay.REPLAY_LEDGER_LIMIT", 1)
        async with database.sessions.begin() as session:
            for _ in range(3):
                session.add(
                    DeletionLedgerRecord(
                        entity_kind="message",
                        entity_id=str(uuid7()),
                        deleted_ids=[],
                        requested_by="test",
                    )
                )
        report = await replay_deletions(database, dry_run=True)
        assert report.ledger_rows == 3
    finally:
        await service.drain_background_work()
        await database.close()


async def test_offline_cli_dry_run_does_not_import_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.replay_deletions import run

    service, database, _ = await _summary_service(tmp_path)
    journal = DeletionJournal(tmp_path / "journal.jsonl")
    try:
        owner = await create_user(database)
        conversation = await service.create_conversation(user_id=owner.id, title="target")
        await journal.append(
            DeletionIntent(
                entity_kind="message",
                entity_id=str(conversation.id),
                deleted_ids=[],
                created_at=datetime.now(UTC),
            )
        )
        monkeypatch.setenv("ARIA_DATABASE_URL", str(database.engine.url))
        await run(apply=False, journal_path=journal.path)
        async with database.sessions() as session:
            assert await session.get(ConversationRecord, conversation.id) is not None
            assert await session.scalar(select(func.count()).select_from(DeletionLedgerRecord)) == 0
        assert '"conversations": 1' in capsys.readouterr().out
        await run(apply=True, journal_path=journal.path)
        async with database.sessions() as session:
            assert await session.get(ConversationRecord, conversation.id) is None
    finally:
        await service.drain_background_work()
        await database.close()


async def test_native_skill_proposal_requires_live_owned_source(tmp_path: Path) -> None:
    service, database, _ = await _summary_service(tmp_path)
    try:
        owner = await create_user(database)
        conversation = await service.create_conversation(user_id=owner.id, title="source")
        pending = await service.start_turn(
            conversation.id,
            user_id=owner.id,
            text="propose",
            privacy_level=PrivacyLevel.L1,
        )
        store = SkillStore(database)
        proposal = SkillProposal(
            document=SkillDocument(name="native-source", description="test", instructions="test"),
            warnings=[],
            evidence=[],
        )
        with pytest.raises(ValueError, match="source_deleted"):
            await store.save_draft(
                proposal,
                system_name="test",
                source="chat",
                turn_id=str(pending.turn_id),
                source_owner_id=uuid7(),
                allow_active_source=True,
            )
        draft = await store.save_draft(
            proposal,
            system_name="test",
            source="chat",
            turn_id=str(pending.turn_id),
            source_owner_id=owner.id,
            allow_active_source=True,
        )
        assert draft is not None
        await service.delete_conversation(conversation.id, user_id=owner.id)
        with pytest.raises(ValueError, match="source_deleted"):
            await store.save_draft(
                proposal,
                system_name="test",
                source="chat",
                turn_id=str(pending.turn_id),
                source_owner_id=owner.id,
                allow_active_source=True,
            )
        assert await store.drafts() == []
    finally:
        await service.drain_background_work()
        await database.close()
