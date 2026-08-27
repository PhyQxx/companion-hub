from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update

from app.db import (
    Base,
    Database,
    InteractionTurnRecord,
    UserModeRecord,
    create_database,
)
from app.ids import uuid7
from app.runtime import LeaseManager, TurnCoordinator
from app.schemas import PrivacyLevel


@pytest.fixture
def tmp_db(tmp_path: Any) -> Database:
    db = create_database(f"sqlite+aiosqlite:///{tmp_path / 'test_runtime.db'}")
    return db


@pytest.fixture
async def database(tmp_db: Database) -> Database:
    async with tmp_db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return tmp_db


class FakeChatService:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.cancelled: list[dict[str, Any]] = []

    async def start_turn(
        self,
        conversation_id: UUID,
        *,
        user_id: UUID,
        text: str,
        privacy_level: PrivacyLevel,
        max_context_messages: int | None = None,
        client_location: object | None = None,
    ) -> Any:
        pending = SimpleNamespace(
            turn_id=uuid7(),
            generation_id=uuid7(),
            turn_seq=1,
            conversation_id=conversation_id,
            user_id=user_id,
        )
        self.started.append(
            {
                "conversation_id": conversation_id,
                "user_id": user_id,
                "text": text,
                "privacy_level": privacy_level,
            }
        )
        return pending

    async def cancel_turn(
        self,
        generation_id: UUID,
        *,
        user_id: UUID,
        reason: str,
    ) -> bool:
        self.cancelled.append(
            {"generation_id": generation_id, "user_id": user_id, "reason": reason}
        )
        return True


@pytest.fixture
def fake_chat() -> FakeChatService:
    return FakeChatService()


@pytest.fixture
def lease_mgr(database: Database) -> LeaseManager:
    return LeaseManager(database)


@pytest.fixture
def coordinator(database: Database, fake_chat: FakeChatService) -> TurnCoordinator:
    return TurnCoordinator(database, fake_chat)


class TestLeaseManager:
    async def test_acquire_new_lease(self, lease_mgr: LeaseManager) -> None:
        dev = uuid7()
        res = await lease_mgr.acquire("audio_output", dev, ttl_seconds=60)
        assert res.acquired is True
        assert res.holder_device_id == dev
        assert res.epoch == 1

    async def test_acquire_preempts(self, lease_mgr: LeaseManager) -> None:
        dev_a = uuid7()
        dev_b = uuid7()
        await lease_mgr.acquire("audio_output", dev_a, ttl_seconds=60)
        res = await lease_mgr.acquire("audio_output", dev_b, ttl_seconds=60)
        assert res.acquired is True
        assert res.previous_holder == dev_a
        assert res.epoch == 2

    async def test_renew_only_holder(self, lease_mgr: LeaseManager) -> None:
        dev = uuid7()
        await lease_mgr.acquire("audio_output", dev, ttl_seconds=10)
        res = await lease_mgr.renew("audio_output", dev, ttl_seconds=60)
        assert res.acquired is True
        # non-holder renew fails
        other = uuid7()
        fail = await lease_mgr.renew("audio_output", other, ttl_seconds=60)
        assert fail.acquired is False
        assert fail.reason_code == "not_holder"

    async def test_release(self, lease_mgr: LeaseManager) -> None:
        dev = uuid7()
        await lease_mgr.acquire("audio_output", dev, ttl_seconds=60)
        assert await lease_mgr.release("audio_output", dev) is True
        assert await lease_mgr.current_holder("audio_output") is None

    async def test_expire_stale(self, lease_mgr: LeaseManager) -> None:
        dev = uuid7()
        await lease_mgr.acquire("audio_output", dev, ttl_seconds=0.001)
        import asyncio

        await asyncio.sleep(0.05)
        expired = await lease_mgr.expire_stale()
        assert len(expired) == 1
        assert expired[0].reason_code == "expired"

    async def test_current_holder(self, lease_mgr: LeaseManager) -> None:
        dev = uuid7()
        await lease_mgr.acquire("microphone", dev, ttl_seconds=60)
        holder = await lease_mgr.current_holder("microphone")
        assert holder is not None
        assert holder.holder_device_id == dev


class TestTurnCoordinator:
    async def test_create_text_turn(
        self, coordinator: TurnCoordinator, fake_chat: FakeChatService
    ) -> None:
        conv = uuid7()
        user = uuid7()
        ctx = await coordinator.create_turn(
            conversation_id=conv,
            user_id=user,
            text="你好",
            privacy_level=PrivacyLevel.L1,
            mode="text",
        )
        assert ctx.conversation_id == conv
        assert ctx.state == "accepted"
        assert ctx.state_version == 1
        assert len(fake_chat.started) == 1

    async def test_create_voice_turn(
        self, coordinator: TurnCoordinator, fake_chat: FakeChatService
    ) -> None:
        conv = uuid7()
        user = uuid7()
        ctx = await coordinator.create_turn(
            conversation_id=conv,
            user_id=user,
            text="你好",
            privacy_level=PrivacyLevel.L1,
            mode="voice",
        )
        assert ctx.state == "listening"
        assert ctx.state_version == 2

    async def test_create_turn_cancels_old(
        self, database: Database, coordinator: TurnCoordinator, fake_chat: FakeChatService
    ) -> None:
        conv = uuid7()
        user = uuid7()
        # seed an old interruptible turn in DB
        old_gen = uuid7()
        async with database.sessions.begin() as session:
            session.add(
                InteractionTurnRecord(
                    id=uuid7(),
                    conversation_id=conv,
                    turn_seq=1,
                    generation_id=old_gen,
                    state="thinking",
                    state_version=1,
                    input_message_id=uuid7(),
                )
            )
        await coordinator.create_turn(
            conversation_id=conv,
            user_id=user,
            text="新的一轮",
            privacy_level=PrivacyLevel.L1,
            mode="text",
        )
        assert any(c["reason"] == "new_turn" for c in fake_chat.cancelled)

    async def test_transition_success(
        self, database: Database, coordinator: TurnCoordinator
    ) -> None:
        turn_id = uuid7()
        async with database.sessions.begin() as session:
            session.add(
                InteractionTurnRecord(
                    id=turn_id,
                    conversation_id=uuid7(),
                    turn_seq=1,
                    generation_id=uuid7(),
                    state="accepted",
                    state_version=1,
                    input_message_id=uuid7(),
                )
            )
        ok = await coordinator.transition(turn_id, 1, "thinking")
        assert ok is True
        async with database.sessions() as session:
            rec = await session.scalar(
                select(InteractionTurnRecord).where(InteractionTurnRecord.id == turn_id)
            )
            assert rec is not None
            assert rec.state == "thinking"
            assert rec.state_version == 2

    async def test_transition_cas_failure(
        self, database: Database, coordinator: TurnCoordinator
    ) -> None:
        turn_id = uuid7()
        async with database.sessions.begin() as session:
            session.add(
                InteractionTurnRecord(
                    id=turn_id,
                    conversation_id=uuid7(),
                    turn_seq=1,
                    generation_id=uuid7(),
                    state="accepted",
                    state_version=2,
                    input_message_id=uuid7(),
                )
            )
        ok = await coordinator.transition(turn_id, 1, "thinking")
        assert ok is False

    async def test_transition_illegal(
        self, database: Database, coordinator: TurnCoordinator
    ) -> None:
        turn_id = uuid7()
        async with database.sessions.begin() as session:
            session.add(
                InteractionTurnRecord(
                    id=turn_id,
                    conversation_id=uuid7(),
                    turn_seq=1,
                    generation_id=uuid7(),
                    state="completed",
                    state_version=1,
                    input_message_id=uuid7(),
                )
            )
        ok = await coordinator.transition(turn_id, 1, "thinking")
        assert ok is False

    async def test_interrupt(
        self, database: Database, coordinator: TurnCoordinator, fake_chat: FakeChatService
    ) -> None:
        turn_id = uuid7()
        gen = uuid7()
        user = uuid7()
        conv = uuid7()
        async with database.sessions.begin() as session:
            from app.db import ConversationRecord
            session.add(
                ConversationRecord(
                    id=conv,
                    user_id=user,
                    title="test",
                )
            )
            session.add(
                InteractionTurnRecord(
                    id=turn_id,
                    conversation_id=conv,
                    turn_seq=1,
                    generation_id=gen,
                    state="streaming",
                    state_version=1,
                    input_message_id=uuid7(),
                )
            )
        ok = await coordinator.interrupt(turn_id, reason="barge_in")
        assert ok is True
        assert any(c["generation_id"] == gen for c in fake_chat.cancelled)

    async def test_interrupt_non_interruptible(
        self, database: Database, coordinator: TurnCoordinator
    ) -> None:
        turn_id = uuid7()
        async with database.sessions.begin() as session:
            session.add(
                InteractionTurnRecord(
                    id=turn_id,
                    conversation_id=uuid7(),
                    turn_seq=1,
                    generation_id=uuid7(),
                    state="completed",
                    state_version=1,
                    input_message_id=uuid7(),
                )
            )
        ok = await coordinator.interrupt(turn_id)
        assert ok is False

    async def test_is_generation_active(
        self, database: Database, coordinator: TurnCoordinator
    ) -> None:
        gen = uuid7()
        async with database.sessions.begin() as session:
            session.add(
                InteractionTurnRecord(
                    id=uuid7(),
                    conversation_id=uuid7(),
                    turn_seq=1,
                    generation_id=gen,
                    state="thinking",
                    state_version=1,
                    input_message_id=uuid7(),
                )
            )
        assert await coordinator.is_generation_active(gen) is True
        # transition to completed
        async with database.sessions.begin() as session:
            await session.execute(
                update(InteractionTurnRecord)
                .where(InteractionTurnRecord.generation_id == gen)
                .values(state="completed", completed_at=datetime.now(UTC))
            )
        assert await coordinator.is_generation_active(gen) is False

    async def test_reject_stale(self, database: Database, coordinator: TurnCoordinator) -> None:
        gen = uuid7()
        async with database.sessions.begin() as session:
            session.add(
                InteractionTurnRecord(
                    id=uuid7(),
                    conversation_id=uuid7(),
                    turn_seq=1,
                    generation_id=gen,
                    state="cancelled",
                    state_version=1,
                    input_message_id=uuid7(),
                    completed_at=datetime.now(UTC),
                )
            )
        assert await coordinator.reject_stale(gen) is True

    async def test_reject_stale_active(
        self, database: Database, coordinator: TurnCoordinator
    ) -> None:
        gen = uuid7()
        async with database.sessions.begin() as session:
            session.add(
                InteractionTurnRecord(
                    id=uuid7(),
                    conversation_id=uuid7(),
                    turn_seq=1,
                    generation_id=gen,
                    state="streaming",
                    state_version=1,
                    input_message_id=uuid7(),
                )
            )
        assert await coordinator.reject_stale(gen) is False

    async def test_recover_after_restart(
        self, database: Database, coordinator: TurnCoordinator
    ) -> None:
        async with database.sessions.begin() as session:
            for state in (
                "accepted",
                "listening",
                "thinking",
                "streaming",
                "speaking",
                "interrupted",
            ):
                session.add(
                    InteractionTurnRecord(
                        id=uuid7(),
                        conversation_id=uuid7(),
                        turn_seq=1,
                        generation_id=uuid7(),
                        state=state,
                        state_version=1,
                        input_message_id=uuid7(),
                    )
                )
            session.add(
                InteractionTurnRecord(
                    id=uuid7(),
                    conversation_id=uuid7(),
                    turn_seq=2,
                    generation_id=uuid7(),
                    state="completed",
                    state_version=1,
                    input_message_id=uuid7(),
                    completed_at=datetime.now(UTC),
                )
            )
        count = await coordinator.recover_after_restart()
        assert count == 6
        async with database.sessions() as session:
            unsafe = await session.scalars(
                select(InteractionTurnRecord).where(
                    InteractionTurnRecord.state.in_(
                        {
                            "accepted",
                            "listening",
                            "thinking",
                            "streaming",
                            "speaking",
                            "interrupted",
                        }
                    )
                )
            )
            assert len(unsafe.all()) == 0

    async def test_user_mode_priority(
        self, database: Database, coordinator: TurnCoordinator
    ) -> None:
        user = uuid7()
        await coordinator.set_user_mode(user, "available", source="default", priority=10)
        await coordinator.set_user_mode(user, "dnd", source="user", priority=90)
        mode = await coordinator.current_user_mode(user)
        assert mode == "dnd"

    async def test_user_mode_expired_ignored(
        self, database: Database, coordinator: TurnCoordinator
    ) -> None:
        user = uuid7()
        past = datetime.now(UTC) - timedelta(hours=1)
        async with database.sessions.begin() as session:
            session.add(
                UserModeRecord(
                    user_id=user,
                    mode="dnd",
                    source="test",
                    priority=90,
                    expires_at=past,
                )
            )
        mode = await coordinator.current_user_mode(user)
        assert mode is None

    async def test_audio_lease_flow(self, database: Database, coordinator: TurnCoordinator) -> None:
        dev = uuid7()
        gen = uuid7()
        res = await coordinator.acquire_audio_lease(dev, gen, ttl_seconds=60)
        assert res.acquired is True
        holder = await coordinator._leases.current_holder("audio_output")
        assert holder is not None
        assert holder.holder_device_id == dev
        assert await coordinator.release_audio_lease(dev) is True

    async def test_microphone_lease(self, database: Database, coordinator: TurnCoordinator) -> None:
        dev = uuid7()
        res = await coordinator.acquire_microphone(dev, ttl_seconds=60)
        assert res.acquired is True
        assert await coordinator.release_microphone(dev) is True
