from pathlib import Path

import pytest
from test_chat import _summary_service, create_user

from app.chat import TurnCancelled
from app.ids import uuid7
from app.schemas import PrivacyLevel


async def test_chat_run_has_ordered_events_and_owner_isolation(tmp_path: Path) -> None:
    service, database, _ = await _summary_service(tmp_path)
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="run")
        result = await service.send_message(
            conversation.id, user_id=user.id, text="hello", privacy_level=PrivacyLevel.L1
        )
        run_id = result.assistant_message.turn_id
        run = await service.runs.get(run_id, user_id=user.id)
        assert run.status == "succeeded"
        events = await service.runs.events(run_id, user_id=user.id)
        assert [event.kind for event in events] == ["run.accepted", "run.running", "run.succeeded"]
        assert [event.seq for event in events] == [1, 2, 3]
        assert len(await service.runs.events(run_id, user_id=user.id, after_seq=2)) == 1
        with pytest.raises(LookupError):
            await service.runs.get(run_id, user_id=uuid7())
        with pytest.raises(LookupError):
            await service.runs.events(run_id, user_id=uuid7())
        assert await service.runs.list_runs(user_id=uuid7()) == []
        assert not await service.cancel_run(run_id, user_id=user.id)
        assert (await service.runs.get(run_id, user_id=user.id)).status == "succeeded"
    finally:
        await service.drain_background_work()
        await database.close()


async def test_cancelled_run_cannot_be_completed_and_delete_removes_events(tmp_path: Path) -> None:
    service, database, requests = await _summary_service(tmp_path)
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="cancel")
        pending = await service.start_turn(
            conversation.id, user_id=user.id, text="hello", privacy_level=PrivacyLevel.L1
        )
        assert await service.cancel_run(pending.turn_id, user_id=user.id)
        run = await service.runs.get(pending.turn_id, user_id=user.id)
        assert run.status == "cancelled" and run.cancel_epoch == 1

        async def delta(value: str) -> None:
            pass

        with pytest.raises(TurnCancelled):
            await service.run_stream(pending, delta)
        assert requests == []
        await service.delete_conversation(conversation.id, user_id=user.id)
        assert await service.runs.list_runs(user_id=user.id) == []
        with pytest.raises(LookupError):
            await service.runs.events(pending.turn_id, user_id=user.id)
    finally:
        await service.drain_background_work()
        await database.close()


async def test_recovery_cancels_run_without_manufacturing_reply(tmp_path: Path) -> None:
    service, database, requests = await _summary_service(tmp_path)
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="recovery")
        pending = await service.start_turn(
            conversation.id, user_id=user.id, text="hello", privacy_level=PrivacyLevel.L1
        )
        await service.recover_incomplete_turns()
        run = await service.runs.get(pending.turn_id, user_id=user.id)
        assert run.status == "cancelled"
        assert requests == []
        assert len(await service.list_messages(conversation.id, user_id=user.id)) == 1
    finally:
        await service.drain_background_work()
        await database.close()


@pytest.mark.parametrize("deleted", [False, True])
async def test_postcommit_survives_restart_and_deleted_source_stops_derivation(
    tmp_path: Path,
    deleted: bool,
) -> None:
    from sqlalchemy import select
    from test_chat import FakeRouter

    from app.chat import ChatService
    from app.db import JobRecord
    from app.memory import MemoryIngester, MemoryStore, RuleBasedExtractor

    service, database, requests = await _summary_service(tmp_path)
    memory_store = MemoryStore(database)
    service._memory_store = memory_store
    service._memory_ingester = MemoryIngester(memory_store)
    service._postcommit._stop.set()  # Simulate commit followed by process exit before delivery.
    restarted = ChatService(
        database,
        service._config_store,
        memory_store=memory_store,
        memory_extractor=RuleBasedExtractor(),
        router_builder=lambda config: FakeRouter("test", requests),
    )
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="restart")
        await service.send_message(
            conversation.id, user_id=user.id, text="我不吃香菜。", privacy_level=PrivacyLevel.L1
        )
        async with database.sessions() as session:
            jobs = list(await session.scalars(select(JobRecord)))
            assert len(jobs) == 1 and jobs[0].status == "queued"
            assert "香菜" not in str(jobs[0].input)
        if deleted:
            await service.delete_conversation(conversation.id, user_id=user.id)
        await restarted._postcommit.process_ready()
        first = await memory_store.list_memories(user_id=user.id)
        assert bool(first) is not deleted
        await restarted._postcommit.process_ready()
        assert [memory.id for memory in await memory_store.list_memories(user_id=user.id)] == [
            memory.id for memory in first
        ]
        async with database.sessions() as session:
            job = await session.get(JobRecord, jobs[0].id)
            assert job is not None
            assert job.status == ("cancelled" if deleted else "succeeded")
    finally:
        await service.drain_background_work()
        await restarted.drain_background_work()
        await database.close()


async def test_postcommit_enqueue_failure_rolls_back_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import func, select

    from app.db import JobRecord, MessageRecord
    from app.memory import MemoryIngester, MemoryStore

    service, database, _ = await _summary_service(tmp_path)
    service._memory_ingester = MemoryIngester(MemoryStore(database))

    async def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected_enqueue_failure")

    monkeypatch.setattr(service._postcommit.engine, "submit_in_session", fail)
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="rollback")
        with pytest.raises(RuntimeError, match="injected_enqueue_failure"):
            await service.send_message(
                conversation.id, user_id=user.id, text="hello", privacy_level=PrivacyLevel.L1
            )
        async with database.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(MessageRecord)
                    .where(MessageRecord.role == "assistant")
                )
                == 0
            )
            assert await session.scalar(select(func.count()).select_from(JobRecord)) == 0
    finally:
        await service.drain_background_work()
        await database.close()


async def test_client_request_id_replays_reply_and_rejects_changed_input(tmp_path: Path) -> None:
    service, database, requests = await _summary_service(tmp_path)
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="retry")
        first = await service.send_message(
            conversation.id,
            user_id=user.id,
            text="hello",
            privacy_level=PrivacyLevel.L1,
            client_request_id="request-1",
        )
        replay = await service.send_message(
            conversation.id,
            user_id=user.id,
            text="hello",
            privacy_level=PrivacyLevel.L1,
            client_request_id="request-1",
        )
        assert replay.assistant_message.id == first.assistant_message.id
        assert len(requests) == 1
        assert len(await service.list_messages(conversation.id, user_id=user.id)) == 2
        with pytest.raises(ValueError, match="request_idempotency_conflict"):
            await service.send_message(
                conversation.id,
                user_id=user.id,
                text="changed",
                privacy_level=PrivacyLevel.L1,
                client_request_id="request-1",
            )
        assert len(requests) == 1
    finally:
        await service.drain_background_work()
        await database.close()


async def test_client_request_id_does_not_restart_accepted_turn(tmp_path: Path) -> None:
    service, database, requests = await _summary_service(tmp_path)
    try:
        user = await create_user(database)
        conversation = await service.create_conversation(user_id=user.id, title="in flight")
        await service.start_turn(
            conversation.id,
            user_id=user.id,
            text="hello",
            privacy_level=PrivacyLevel.L1,
            client_request_id="request-1",
        )
        with pytest.raises(ValueError, match="request_already_accepted"):
            await service.send_message(
                conversation.id,
                user_id=user.id,
                text="hello",
                privacy_level=PrivacyLevel.L1,
                client_request_id="request-1",
            )
        assert requests == []
        assert len(await service.list_messages(conversation.id, user_id=user.id)) == 1
    finally:
        await service.drain_background_work()
        await database.close()
