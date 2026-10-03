"""PCM limits reject input before decoding and discard aborted capture state."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from test_voice_streaming_privacy import FactoryRecognizer, session_fixture
from test_voice_websocket import RecordingWebSocket, _build, _receive_until, loud_frames

from app.api.voice_ws import AsrPrefetch, VoiceSession, VoiceWebSocketManager
from app.chat import ChatService
from app.ids import uuid7
from app.voice.contracts import VadEvent
from app.voice.factory import StaticVoiceSource


class CountingVad:
    backend = "fixture"
    speaking = False

    def __init__(self) -> None:
        self.voiced_calls = 0
        self.feeds = 0
        self.resets = 0

    def is_voiced(self, pcm: bytes) -> bool:
        self.voiced_calls += 1
        return False

    def feed(self, pcm: bytes) -> VadEvent | None:
        self.feeds += 1
        return None

    def force_end(self) -> VadEvent | None:
        self.resets += 1
        return None


def setup() -> tuple[
    VoiceWebSocketManager, VoiceSession, RecordingWebSocket, FactoryRecognizer, CountingVad
]:
    raw_session, raw_socket = session_fixture()
    session, socket = cast(VoiceSession, raw_session), cast(RecordingWebSocket, raw_socket)
    recognizer = FactoryRecognizer(parent_local=True, child_local=True)
    manager = VoiceWebSocketManager(
        cast(ChatService, object()), voice_source=StaticVoiceSource(recognizer, None)
    )
    vad = CountingVad()
    session.vad = vad
    return manager, session, socket, recognizer, vad


@pytest.mark.parametrize(
    "pcm", [b"\x01\x00" * 32769, b"\x01\x00\x01"], ids=["oversized", "unaligned"]
)
async def test_invalid_frame_is_rejected_before_vad_and_decoder(pcm: bytes) -> None:
    manager, session, socket, recognizer, vad = setup()
    session.collecting = session.ptt_active = True
    await manager._start_streamer(session, None)
    await manager._on_audio(session, pcm)
    assert recognizer.child.fed_bytes == 0
    assert vad.voiced_calls == vad.feeds == 0
    assert not session.collecting and not session.ptt_active
    assert session.utterance == b"" and session.utterance_streamer is None
    assert any(message.get("reason") == "invalid_audio_frame" for message in socket.texts)


async def test_pcm_over_120_seconds_is_discarded_without_finalizing() -> None:
    manager, session, socket, recognizer, vad = setup()
    session.collecting = session.ptt_active = True
    await manager._start_streamer(session, None)
    # 16 kHz mono PCM16: allow exactly 120 seconds, reject the next sample.
    session.utterance.extend(b"\x01\x00" * (16000 * 120))
    await manager._on_audio(session, b"\x01\x00")
    assert not session.collecting and not session.ptt_active
    assert session.utterance == b"" and session.utterance_streamer is None
    assert recognizer.child.fed_bytes == recognizer.child.finalize_calls == 0
    assert vad.resets == 1
    assert any(message.get("reason") == "utterance_too_large" for message in socket.texts)
    assert session.turn_task is None


async def test_disconnect_discards_all_pending_pcm_and_capture_flags() -> None:
    manager, session, _socket, recognizer, vad = setup()
    session.collecting = session.ptt_active = session.wake_armed = True
    session.utterance.extend(b"\x01\x00" * 320)
    await manager._start_streamer(session, None)
    await manager.disconnect(session)
    assert session.utterance == b"" and session.utterance_streamer is None
    assert not session.collecting and not session.ptt_active and not session.wake_armed
    assert recognizer.child.fed_bytes == recognizer.child.finalize_calls == 0
    assert vad.resets == 1


async def test_exact_frame_boundary_is_allowed() -> None:
    manager, session, socket, recognizer, _vad = setup()
    session.collecting = session.ptt_active = True
    await manager._start_streamer(session, None)
    await manager._on_audio(session, b"\x01\x00" * 32768)
    assert len(session.utterance) == 65536 and recognizer.child.fed_bytes == 65536
    assert not any(message.get("type") == "voice.error" for message in socket.texts)


async def test_exact_utterance_boundary_is_allowed_then_next_sample_rejected() -> None:
    manager, session, _socket, recognizer, _vad = setup()
    session.collecting = session.ptt_active = True
    await manager._start_streamer(session, None)
    session.utterance.extend(b"\x01\x00" * (16000 * 120 - 1))
    await manager._on_audio(session, b"\x01\x00")
    assert len(session.utterance) == 3_840_000 and recognizer.child.fed_bytes == 2
    await manager._on_audio(session, b"\x01\x00")
    assert len(session.utterance) == 0 and recognizer.child.fed_bytes == 2


async def test_empty_frame_does_not_produce_a_partial_or_advance_vad() -> None:
    manager, session, socket, recognizer, vad = setup()
    session.collecting = session.ptt_active = True
    await manager._start_streamer(session, None)
    await manager._on_audio(session, b"")
    assert not socket.texts
    assert vad.voiced_calls == vad.feeds == recognizer.child.fed_bytes == 0


async def test_rejected_frame_cancels_prefetch_and_releases_microphone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, session, _socket, recognizer, _vad = setup()
    released: list[VoiceSession] = []

    async def release(value: VoiceSession) -> None:
        released.append(value)

    async def waiting() -> str:
        await asyncio.Future[None]()
        return "must not be accepted"

    task = asyncio.create_task(waiting())
    session.asr_prefetch = AsrPrefetch(recognizer, task)
    monkeypatch.setattr(manager, "_release_microphone", release)
    try:
        await manager._on_audio(session, b"\x01")
        assert released == [session] and session.asr_prefetch is None
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_new_explicit_capture_recovers_after_a_rejected_frame() -> None:
    manager, session, _socket, recognizer, _vad = setup()
    session.collecting = session.ptt_active = True
    await manager._start_streamer(session, None)
    await manager._on_audio(session, b"\x01")
    await manager._on_control(session, '{"type":"utterance.begin"}')
    await manager._on_audio(session, b"\x01\x00" * 32)
    assert session.collecting and session.ptt_active and session.utterance_streamer is not None
    assert len(session.utterance) == recognizer.child.fed_bytes == 64
    assert recognizer.created == 2


async def test_queued_ptt_frames_cannot_restart_a_rejected_capture() -> None:
    manager, session, _socket, recognizer, vad = setup()
    session.collecting = session.ptt_active = True
    await manager._start_streamer(session, None)
    await manager._on_audio(session, b"\x01")
    for _ in range(10):
        await manager._on_audio(session, b"\x01\x00" * 320)
    assert session.ptt_rejected and not session.collecting
    assert vad.voiced_calls == vad.feeds == recognizer.child.fed_bytes == 0


async def test_invalid_audio_cannot_authorize_interrupting_an_existing_turn() -> None:
    manager, session, _socket, _recognizer, vad = setup()
    session.generation_id = uuid7()
    task = asyncio.create_task(asyncio.sleep(5))
    session.turn_task = task
    try:
        await manager._on_audio(session, b"\x01")
        assert vad.voiced_calls == 0 and not task.cancelled() and not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_abort_discards_partial_from_inflight_decoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, session, socket, recognizer, _vad = setup()
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def feed(pcm: bytes) -> str:
        loop.call_soon_threadsafe(started.set)
        assert release.wait(3)
        return "late partial"

    monkeypatch.setattr(recognizer.child, "feed", feed)
    session.collecting = session.ptt_active = True
    await manager._start_streamer(session, None)
    task = asyncio.create_task(manager._feed_streamer(session, b"\x01\x00" * 32))
    try:
        await asyncio.wait_for(started.wait(), 1)
        await manager._on_audio(session, b"\x01")
        release.set()
        await asyncio.wait_for(task, 1)
        assert not any(
            message.get("type") == "voice.partial_transcript" for message in socket.texts
        )
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_websocket_remains_usable_after_oversized_pcm(tmp_path: Path) -> None:
    recognizer = FactoryRecognizer(parent_local=True, child_local=True)
    app, token, conversation_id = _build(tmp_path, recognizer=recognizer)
    with TestClient(app) as client, client.websocket_connect("/ws/voice") as websocket:
        websocket.send_json({"type": "authenticate", "access_token": token})
        websocket.send_json(
            {
                "type": "voice.hello",
                "conversation_id": conversation_id,
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
            }
        )
        _receive_until(websocket, "voice.ready")
        websocket.send_json({"type": "utterance.begin"})
        websocket.send_bytes(b"\x01\x00" * 32769)
        events, _ = _receive_until(websocket, "voice.error")
        assert events[-1]["reason"] == "invalid_audio_frame"
        assert recognizer.child.fed_bytes == 0
        websocket.send_json({"type": "utterance.begin"})
        websocket.send_bytes(loud_frames(10))
        websocket.send_json({"type": "utterance.end"})
        events, _ = _receive_until(
            websocket, "reply.committed", "turn.failed", "voice.asr_unavailable", "voice.error"
        )
        assert events[-1]["type"] == "reply.committed"
    assert recognizer.child.fed_bytes == len(loud_frames(10))
