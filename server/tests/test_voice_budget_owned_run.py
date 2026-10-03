"""Synthetic speech failure leaves the actual owned chat Run failed, without a reply."""

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_run_cancel_fence import prepared
from test_voice_budget_terminal import AudioStream, Synthesizer, setup
from test_voice_websocket import (
    FakeRecognizer,
    StreamingBackend,
    SyntheticVoiceSourceGuard,
    config_yaml,
)

from app.api.voice_ws import VoiceWebSocketManager
from app.chat import ChatService
from app.config import DatabaseConfigStore
from app.db import AppUserRecord, MessageRecord, TaskRunRecord
from app.harness.budget import BudgetDenied
from app.voice.factory import StaticVoiceSource
from app.voice.failover import TtsProviderChain


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_tts_denial_during_owned_chat_does_not_commit_reply(
    backend: str,
    late: bool,
    cleanup_fails: bool,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner = uuid4()
        async with storage.database.sessions.begin() as seed_session:
            seed_session.add(AppUserRecord(id=owner, display_name="fixture", status="active"))
        path = tmp_path / "synthetic-voice.yaml"
        path.write_text(config_yaml())
        config = DatabaseConfigStore(storage.database, path)
        await config.load()
        chat = ChatService(storage.database, config, router_builder=lambda _: StreamingBackend())
        conversation = await chat.create_conversation(user_id=owner, title="synthetic voice")
        error = BudgetDenied("synthetic_audio_budget_denied")
        first = Synthesizer(AudioStream(error, late=late, cleanup_fails=cleanup_fails))
        backup = Synthesizer(AudioStream(None))
        chain = TtsProviderChain([first, backup])
        recognizer = FakeRecognizer("synthetic utterance")
        _, session, socket = setup()
        session.principal = replace(session.principal, user_id=owner)
        session.conversation_id = conversation.id
        manager = VoiceWebSocketManager(
            chat,
            source_guard=SyntheticVoiceSourceGuard(),
            voice_source=StaticVoiceSource(recognizer, chain),
        )
        await manager._run_utterance(session, b"\x01\x00" * 16000, recognizer, chain)
        assert [
            message["reason_code"] for message in socket.texts if message["type"] == "turn.failed"
        ] == [error.reason_code]
        assert not any(
            message["type"] in {"reply.committed", "voice.sentence.end"} for message in socket.texts
        )
        assert first.calls == first.stream.closed == 1 and backup.calls == 0
        assert not chain._blocked_until and len(socket.audio) == int(late)
        async with storage.database.sessions() as database_session:
            runs = list(
                await database_session.scalars(
                    select(TaskRunRecord).where(TaskRunRecord.user_id == owner)
                )
            )
            replies = list(
                await database_session.scalars(
                    select(MessageRecord).where(
                        MessageRecord.conversation_id == conversation.id,
                        MessageRecord.role == "assistant",
                    )
                )
            )
        assert len(runs) == 1 and runs[0].status == "failed" and not replies
    finally:
        await storage.close()
