"""Private audio must be checked before streaming and full-ASR fallback."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_voice_websocket import (
    FakeStreamingRecognizer,
    SyntheticVoiceSourceGuard,
    _build,
    _receive_until,
    loud_frames,
)

from app.api.voice_ws import UtteranceStreamer
from app.schemas import PrivacyLevel
from app.voice.contracts import StreamingSpeechRecognizer


class FactoryRecognizer(FakeStreamingRecognizer):
    def __init__(self, *, parent_local: bool, child_local: bool) -> None:
        super().__init__()
        self.runs_local = parent_local
        self.child = FakeStreamingRecognizer()
        self.child.runs_local = child_local
        self.created = 0

    def create_session(self) -> StreamingSpeechRecognizer:
        self.created += 1
        return self.child


@pytest.mark.parametrize("privacy", [PrivacyLevel.L2, PrivacyLevel.L3])
@pytest.mark.parametrize("parent_local", [False, True])
async def test_private_stream_checks_parent_and_created_session(
    privacy: PrivacyLevel, parent_local: bool
) -> None:
    recognizer = FactoryRecognizer(parent_local=parent_local, child_local=False)
    streamer = UtteranceStreamer(recognizer, privacy_level=privacy)
    assert not streamer.active
    assert await streamer.feed(b"\x01\x00" * 32) is None
    assert await streamer.finalize() is None
    assert recognizer.created == int(parent_local)
    assert recognizer.fed_bytes == 0 and recognizer.child.fed_bytes == 0


async def test_public_stream_uses_the_factory_session() -> None:
    recognizer = FactoryRecognizer(parent_local=False, child_local=False)
    streamer = UtteranceStreamer(recognizer, privacy_level=PrivacyLevel.L1)
    assert streamer.active
    await streamer.feed(b"\x01\x00" * 32)
    assert await streamer.finalize() == recognizer.child._final
    assert recognizer.child.fed_bytes == 64 and recognizer.fed_bytes == 0


@pytest.mark.parametrize("privacy", [PrivacyLevel.L2, PrivacyLevel.L3])
def test_websocket_private_audio_never_reaches_cloud_stream_or_full_fallback(
    tmp_path: Path, privacy: PrivacyLevel
) -> None:
    recognizer = FactoryRecognizer(parent_local=False, child_local=False)
    app, token, conversation_id = _build(tmp_path, recognizer=recognizer)
    with TestClient(app) as client, client.websocket_connect("/ws/voice") as websocket:
        websocket.send_json({"type": "authenticate", "access_token": token})
        websocket.send_json(
            {
                "type": "voice.hello",
                "conversation_id": conversation_id,
                "privacy_level": str(privacy),
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
            }
        )
        _receive_until(websocket, "voice.ready")
        websocket.send_json({"type": "utterance.begin"})
        websocket.send_bytes(loud_frames(10))
        websocket.send_json({"type": "utterance.end"})
        events, _ = _receive_until(websocket, "voice.asr_unavailable")
    assert events[-1]["reason"] == "local_asr_required"
    assert recognizer.created == recognizer.fed_bytes == recognizer.transcribe_calls == 0


def session_fixture() -> tuple[object, object]:
    from datetime import UTC, datetime, timedelta
    from typing import cast

    from fastapi import WebSocket
    from test_voice_websocket import RecordingWebSocket

    from app.api.voice_ws import VoiceSession
    from app.auth import ChatPrincipal
    from app.ids import uuid7

    socket = RecordingWebSocket()
    session = VoiceSession(
        websocket=cast(WebSocket, socket),
        principal=ChatPrincipal(
            session_id=uuid7(),
            user_id=uuid7(),
            display_name="Fixture",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
        conversation_id=uuid7(),
        wake_word=None,
    )
    return session, socket


async def test_privacy_change_drops_inflight_partial_and_stops_further_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading
    from typing import cast

    from test_voice_websocket import RecordingWebSocket

    from app.api.voice_ws import VoiceSession, VoiceWebSocketManager
    from app.chat import ChatService
    from app.voice.factory import StaticVoiceSource

    recognizer = FactoryRecognizer(parent_local=False, child_local=False)
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = recognizer.child.feed

    def feed(pcm: bytes) -> str:
        loop.call_soon_threadsafe(started.set)
        assert release.wait(3)
        original(pcm)
        return "late partial from original privacy"

    monkeypatch.setattr(recognizer.child, "feed", feed)
    raw_session, raw_socket = session_fixture()
    session, socket = cast(VoiceSession, raw_session), cast(RecordingWebSocket, raw_socket)
    manager = VoiceWebSocketManager(
        cast(ChatService, object()),
        source_guard=SyntheticVoiceSourceGuard(),
        voice_source=StaticVoiceSource(recognizer, None),
    )
    await manager._start_streamer(session, None)
    task = asyncio.create_task(manager._feed_streamer(session, b"\x01\x00" * 32))
    try:
        await asyncio.wait_for(started.wait(), 2)
        session.privacy_level = PrivacyLevel.L2
        release.set()
        await asyncio.wait_for(task, 2)
        assert socket.texts == []
        await manager._feed_streamer(session, b"\x02\x00" * 32)
        assert recognizer.child.fed_bytes == 64
        assert session.utterance_streamer is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_privacy_change_before_finalize_does_not_call_cloud_finalize() -> None:
    from typing import cast

    from app.api.voice_ws import VoiceSession, VoiceWebSocketManager
    from app.chat import ChatService
    from app.voice.factory import StaticVoiceSource

    recognizer = FactoryRecognizer(parent_local=False, child_local=False)
    raw_session, _ = session_fixture()
    session = cast(VoiceSession, raw_session)
    manager = VoiceWebSocketManager(
        cast(ChatService, object()),
        source_guard=SyntheticVoiceSourceGuard(),
        voice_source=StaticVoiceSource(recognizer, None),
    )
    await manager._start_streamer(session, b"\x01\x00" * 32)
    session.privacy_level = PrivacyLevel.L3
    assert await manager._finalize_streamer(session) == (None, None, None)
    assert recognizer.child.finalize_calls == 0


async def test_privacy_change_during_full_asr_rejects_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from typing import cast

    from test_voice_websocket import RecordingWebSocket

    from app.api.voice_ws import VoiceSession, VoiceWebSocketManager
    from app.chat import ChatService
    from app.voice.factory import StaticVoiceSource

    recognizer = FactoryRecognizer(parent_local=False, child_local=False)
    started, release = asyncio.Event(), asyncio.Event()

    async def transcribe(pcm: bytes, *, sample_rate: int, language: str | None) -> str:
        started.set()
        await release.wait()
        return "late transcript from original privacy"

    monkeypatch.setattr(recognizer, "transcribe", transcribe)
    raw_session, raw_socket = session_fixture()
    session, socket = cast(VoiceSession, raw_session), cast(RecordingWebSocket, raw_socket)
    manager = VoiceWebSocketManager(
        cast(ChatService, object()),
        source_guard=SyntheticVoiceSourceGuard(),
        voice_source=StaticVoiceSource(recognizer, None),
    )
    task = asyncio.create_task(manager._run_utterance(session, b"\x01\x00" * 32, recognizer, None))
    try:
        await asyncio.wait_for(started.wait(), 2)
        session.privacy_level = PrivacyLevel.L2
        release.set()
        await asyncio.wait_for(task, 2)
        assert socket.texts == [{"type": "voice.asr_unavailable", "reason": "asr_privacy_changed"}]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("privacy", [PrivacyLevel.L2, PrivacyLevel.L3])
async def test_private_tts_selection_never_attempts_cloud_provider(privacy: PrivacyLevel) -> None:
    from collections.abc import AsyncIterator

    from app.voice.contracts import SpeechSynthesizer
    from app.voice.failover import TtsProviderChain

    class Synthesizer:
        mime = "audio/pcm"
        sample_rate = 24000

        def __init__(self, local: bool) -> None:
            self.runs_local, self.calls = local, 0

        async def synthesize(
            self, text: str, *, privacy_level: PrivacyLevel
        ) -> AsyncIterator[bytes]:
            self.calls += 1
            yield b"\x01\x00"

    cloud, local = Synthesizer(False), Synthesizer(True)
    providers: list[SpeechSynthesizer] = [cloud, local]
    selected = await TtsProviderChain(providers).select(
        "private synthetic text", privacy_level=privacy
    )
    assert selected.provider is local
    assert selected.first_chunk == b"\x01\x00" and cloud.calls == 0 and local.calls == 1
    assert [chunk async for chunk in selected.stream] == []


@pytest.mark.parametrize("vendor", ["mimo", "senseaudio", "edge"])
async def test_cloud_tts_adapter_rejects_l3_before_constructing_requests(
    vendor: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    from types import ModuleType
    from typing import cast

    import httpx

    from app.voice.contracts import LocalOnlySynthesizerError, SpeechSynthesizer
    from app.voice.mimo import MiMoTtsSynthesizer
    from app.voice.senseaudio import SenseAudioTtsSynthesizer
    from app.voice.tts import EdgeTtsSynthesizer

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("private TTS attempted to construct an outbound client")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)

    class EdgeModule(ModuleType):
        Communicate = staticmethod(forbidden)

    edge = EdgeModule("edge_tts")
    monkeypatch.setitem(sys.modules, "edge_tts", edge)
    provider = cast(
        SpeechSynthesizer,
        {
            "mimo": MiMoTtsSynthesizer("fixture-secret", base_url="http://invalid.fixture"),
            "senseaudio": SenseAudioTtsSynthesizer(
                "fixture-secret", base_url="http://invalid.fixture"
            ),
            "edge": EdgeTtsSynthesizer(),
        }[vendor],
    )
    stream = provider.synthesize("private synthetic text", privacy_level=PrivacyLevel.L3)
    with pytest.raises(LocalOnlySynthesizerError):
        await stream.__anext__()


async def test_private_tts_without_local_candidate_declines_before_fallback() -> None:
    from test_voice_websocket import FakeSynthesizer

    from app.voice.contracts import LocalOnlySynthesizerError
    from app.voice.failover import TtsProviderChain

    chain = TtsProviderChain([FakeSynthesizer(runs_local=False)])
    with pytest.raises(LocalOnlySynthesizerError):
        await chain.select("private synthetic text", privacy_level=PrivacyLevel.L3)
