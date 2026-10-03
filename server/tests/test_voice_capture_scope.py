"""Captured private audio and queued text cannot inherit a later hello scope."""

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from test_voice_budget_terminal import setup
from test_voice_source_guard import bind, source_fixture
from test_voice_websocket import FakeRecognizer

from app.api.voice_ws import VoiceWebSocketManager
from app.chat import ChatService
from app.db import ConversationRecord
from app.harness.budget import BudgetDenied
from app.ids import uuid7
from app.runs.voice_sources import SqlVoiceSourceGuard
from app.schemas import PrivacyLevel
from app.voice.contracts import SpeechRecognitionUnavailable
from app.voice.factory import StaticVoiceSource
from app.voice.vad import EnergyVad


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("privacy", [PrivacyLevel.L2, PrivacyLevel.L3])
async def test_queued_private_audio_rejects_changed_scope_before_cloud_asr(
    backend: str, privacy: PrivacyLevel, tmp_path: Path
) -> None:
    storage, source = await source_fixture(backend, tmp_path)
    try:
        source = replace(source, privacy_level=privacy)
        _, session, _ = setup()
        bind(session, source)
        session.privacy_level = PrivacyLevel.L1
        recognizer = FakeRecognizer("synthetic private transcript")
        manager = VoiceWebSocketManager(
            cast(ChatService, object()),
            voice_source=StaticVoiceSource(recognizer, None),
            source_guard=SqlVoiceSourceGuard(storage.database),
        )
        with pytest.raises(SpeechRecognitionUnavailable, match="asr_privacy_changed"):
            await manager._transcribe(session, recognizer, b"\x01\x00" * 32, source)
        assert recognizer.calls == 0
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", ["privacy", "conversation", "unchanged"])
async def test_hello_rebind_discards_capture_only_when_its_scope_changes(
    backend: str, change: str, tmp_path: Path
) -> None:
    storage, source = await source_fixture(backend, tmp_path)
    try:
        _, session, socket = setup()
        bind(session, source)
        session.vad = EnergyVad()
        recognizer = FakeRecognizer("")
        manager = VoiceWebSocketManager(
            cast(ChatService, object()),
            voice_source=StaticVoiceSource(recognizer, None),
            source_guard=SqlVoiceSourceGuard(storage.database),
        )
        await manager._on_control(session, '{"type":"utterance.begin"}')
        session.utterance.extend(b"\x01\x00" * 32)
        conversation = source.conversation_id
        if change == "conversation":
            conversation = uuid7()
            async with storage.database.sessions.begin() as sql:
                sql.add(ConversationRecord(id=conversation, user_id=source.user_id, title="Next"))
        await manager._on_hello(
            session,
            {
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
                "conversation_id": str(conversation),
                "privacy_level": "L0" if change == "privacy" else "L1",
            },
        )
        assert session.collecting is (change == "unchanged")
        assert bool(session.utterance) is (change == "unchanged")
        assert session.ptt_active is (change == "unchanged")
        assert recognizer.calls == 0 and socket.texts[-1]["type"] == "voice.ready"
    finally:
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_queued_text_cannot_move_to_a_new_owned_conversation(
    backend: str, tmp_path: Path
) -> None:
    storage, source = await source_fixture(backend, tmp_path)

    class Chat:
        starts = 0

        async def start_turn(self, *args: Any, **kwargs: Any) -> Any:
            self.starts += 1
            raise BudgetDenied("synthetic_stop")

    chat = Chat()
    _, session, socket = setup()
    bind(session, source)
    manager = VoiceWebSocketManager(
        cast(ChatService, chat),
        voice_source=StaticVoiceSource(None, None),
        source_guard=SqlVoiceSourceGuard(storage.database),
    )
    try:
        next_conversation = uuid7()
        async with storage.database.sessions.begin() as sql:
            sql.add(ConversationRecord(id=next_conversation, user_id=source.user_id, title="Next"))
        await manager._on_text_submit(session, {"text": "synthetic queued text"})
        task = session.turn_task
        assert task is not None
        session.conversation_id = next_conversation
        await task
        assert chat.starts == 0 and session.last_transcript is None
        assert socket.texts == [
            {"type": "turn.failed", "generation_id": None, "reason_code": "voice_source_changed"}
        ]
    finally:
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", ["privacy", "conversation"])
async def test_finalizing_capture_never_reinterprets_original_audio_scope(
    backend: str, change: str, tmp_path: Path
) -> None:
    storage, source = await source_fixture(backend, tmp_path)
    _, session, _ = setup()
    bind(session, source)
    session.vad = EnergyVad()
    recognizer = FakeRecognizer("")
    manager = VoiceWebSocketManager(
        cast(ChatService, object()),
        voice_source=StaticVoiceSource(recognizer, None),
        source_guard=SqlVoiceSourceGuard(storage.database),
    )
    try:
        await manager._on_control(session, '{"type":"utterance.begin"}')
        session.utterance.extend(b"\x01\x00" * 16000)
        if change == "privacy":
            session.privacy_level = PrivacyLevel.L0
        else:
            target = uuid7()
            async with storage.database.sessions.begin() as sql:
                sql.add(ConversationRecord(id=target, user_id=source.user_id, title="Next"))
            session.conversation_id = target
        reason = "voice_privacy_changed" if change == "privacy" else "voice_source_changed"
        with pytest.raises(BudgetDenied, match=reason):
            await manager._finalize_utterance(session, explicit=True)
        assert recognizer.calls == 0 and session.turn_task is None
        assert not session.utterance and session.utterance_source is None
    finally:
        await manager.disconnect(session)
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", ["privacy", "conversation"])
async def test_hello_waits_for_old_asr_to_stop_before_rebinding(
    backend: str, change: str, tmp_path: Path
) -> None:
    import asyncio

    storage, source = await source_fixture(backend, tmp_path)
    started, stopped = asyncio.Event(), asyncio.Event()

    class Recognizer(FakeRecognizer):
        async def transcribe(self, pcm: bytes, *, sample_rate: int, language: str | None) -> str:
            self.calls += 1
            started.set()
            try:
                await asyncio.Event().wait()
                return "unused"
            finally:
                stopped.set()

    _, session, socket = setup()
    bind(session, source)
    session.vad = EnergyVad()
    recognizer = Recognizer("")
    manager = VoiceWebSocketManager(
        cast(ChatService, object()),
        voice_source=StaticVoiceSource(recognizer, None),
        source_guard=SqlVoiceSourceGuard(storage.database),
    )
    try:
        target = source.conversation_id
        if change == "conversation":
            target = uuid7()
            async with storage.database.sessions.begin() as sql:
                sql.add(ConversationRecord(id=target, user_id=source.user_id, title="Next"))
        task = asyncio.create_task(
            manager._run_utterance(session, b"\x01\x00" * 32, recognizer, None)
        )
        session.turn_task = task
        await asyncio.wait_for(started.wait(), 1)
        await manager._on_hello(
            session,
            {
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
                "conversation_id": str(target),
                "privacy_level": "L0" if change == "privacy" else "L1",
            },
        )
        assert stopped.is_set() and task.done() and session.turn_task is None
        assert recognizer.calls == 1 and session.conversation_id == target
        assert socket.texts[0]["type"] == "voice.interrupted"
        assert socket.texts[-1]["type"] == "voice.ready"
        assert not any(frame["type"] == "voice.transcript" for frame in socket.texts)
    finally:
        await manager.disconnect(session)
        await storage.close()
