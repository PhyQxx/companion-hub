"""Voice transport must not spend again after terminal source failures."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from test_voice_streaming_privacy import session_fixture
from test_voice_websocket import FakeRecognizer, RecordingWebSocket, SyntheticVoiceSourceGuard

from app.api.voice_ws import AsrPrefetch, VoiceSession, VoiceWebSocketManager
from app.chat import ChatService
from app.harness.budget import BudgetDenied
from app.schemas import PrivacyLevel
from app.voice.factory import StaticVoiceSource
from app.voice.failover import TtsProviderChain


class AudioStream:
    def __init__(
        self, error: BaseException | None, *, late: bool = False, cleanup_fails: bool = False
    ) -> None:
        self.error, self.late, self.cleanup_fails = error, late, cleanup_fails
        self.reads = self.closed = 0

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        self.reads += 1
        if self.late and self.reads == 1:
            return b"\x01\x00" * 16
        if self.error is not None:
            raise self.error
        if self.reads == 1:
            return b"\x01\x00" * 16
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed += 1
        if self.cleanup_fails:
            raise RuntimeError("synthetic stream cleanup failure")


class Synthesizer:
    runs_local = True
    mime = "audio/pcm;rate=24000"
    sample_rate = 24000

    def __init__(self, stream: AudioStream) -> None:
        self.stream, self.calls = stream, 0

    def synthesize(self, text: str, *, privacy_level: PrivacyLevel) -> AsyncIterator[bytes]:
        self.calls += 1
        return self.stream


@pytest.mark.parametrize("privacy", list(PrivacyLevel))
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_tts_budget_denial_does_not_fallback_or_cool_down(
    privacy: PrivacyLevel,
    cleanup_fails: bool,
) -> None:
    error = BudgetDenied("synthetic_audio_budget_denied")
    first = Synthesizer(AudioStream(error, cleanup_fails=cleanup_fails))
    backup = Synthesizer(AudioStream(None))
    chain = TtsProviderChain([first, backup])
    with pytest.raises(BudgetDenied) as captured:
        await chain.select("synthetic speech", privacy_level=privacy)
    assert captured.value is error
    assert first.calls == first.stream.closed == 1 and backup.calls == 0
    assert chain._blocked_until == {}


@pytest.mark.parametrize("failure", ["ordinary", "empty", "cancel"])
async def test_unselected_tts_stream_is_closed_before_fallback(failure: str) -> None:
    error: BaseException = (
        asyncio.CancelledError()
        if failure == "cancel"
        else StopAsyncIteration()
        if failure == "empty"
        else RuntimeError("synthetic source failure")
    )
    first, backup = Synthesizer(AudioStream(error)), Synthesizer(AudioStream(None))
    chain = TtsProviderChain([first, backup])
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await chain.select("synthetic speech", privacy_level=PrivacyLevel.L1)
        assert backup.calls == 0 and not chain._blocked_until
    else:
        selection = await chain.select("synthetic speech", privacy_level=PrivacyLevel.L1)
        assert selection.provider is backup and first.stream.closed == 1
        assert backup.stream.closed == 0
        await backup.stream.aclose()
    assert first.stream.closed == 1


def setup(
    recognizer: FakeRecognizer | None = None, chain: TtsProviderChain | None = None
) -> tuple[
    VoiceWebSocketManager,
    VoiceSession,
    RecordingWebSocket,
]:
    raw_session, raw_socket = session_fixture()
    manager = VoiceWebSocketManager(
        cast(ChatService, object()),
        source_guard=SyntheticVoiceSourceGuard(),
        voice_source=StaticVoiceSource(recognizer, chain),
    )
    return manager, cast(VoiceSession, raw_session), cast(RecordingWebSocket, raw_socket)


async def denied() -> str:
    raise BudgetDenied("synthetic_audio_budget_denied")


async def test_asr_prefetch_denial_is_terminal_in_consumer() -> None:
    recognizer = FakeRecognizer("synthetic fallback transcript")
    manager, session, _ = setup(recognizer)
    session.asr_prefetch = AsrPrefetch(recognizer, asyncio.create_task(denied()))
    with pytest.raises(BudgetDenied, match="synthetic_audio_budget_denied"):
        await manager._consume_asr_prefetch(session, recognizer)
    assert session.asr_prefetch is None and recognizer.calls == 0


async def test_asr_prefetch_denial_emits_failure_without_starting_fallback_turn() -> None:
    recognizer = FakeRecognizer("synthetic fallback transcript")
    manager, session, socket = setup(recognizer)
    session.utterance.extend(b"\x01\x00" * 16000)
    session.asr_prefetch = AsrPrefetch(recognizer, asyncio.create_task(denied()))
    try:
        await manager._finalize_utterance(session, explicit=True)
        assert session.turn_task is None and recognizer.calls == 0
        assert [
            message["reason_code"] for message in socket.texts if message["type"] == "turn.failed"
        ] == ["synthetic_audio_budget_denied"]
    finally:
        if session.turn_task is not None:
            session.turn_task.cancel()
            await asyncio.gather(session.turn_task, return_exceptions=True)


async def test_caller_cancel_while_waiting_for_asr_is_not_swallowed() -> None:
    recognizer = FakeRecognizer("synthetic fallback transcript")
    manager, session, _ = setup(recognizer)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pending() -> str:
        entered.set()
        await release.wait()
        return "synthetic result"

    child = asyncio.create_task(pending())
    session.asr_prefetch = AsrPrefetch(recognizer, child)
    consumer = asyncio.create_task(manager._consume_asr_prefetch(session, recognizer))
    try:
        await entered.wait()
        await asyncio.sleep(0)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert child.cancelled() and recognizer.calls == 0
    finally:
        release.set()
        await asyncio.gather(child, consumer, return_exceptions=True)


async def test_independently_cancelled_prefetch_still_allows_existing_fallback() -> None:
    recognizer = FakeRecognizer("synthetic fallback transcript")
    manager, session, _ = setup(recognizer)
    child = asyncio.create_task(denied())
    child.cancel()
    session.asr_prefetch = AsrPrefetch(recognizer, child)
    assert await manager._consume_asr_prefetch(session, recognizer) is None


async def test_direct_asr_denial_keeps_structured_failure_reason() -> None:
    class Denied(FakeRecognizer):
        async def transcribe(self, pcm: bytes, *, sample_rate: int, language: str | None) -> str:
            self.calls += 1
            raise BudgetDenied("synthetic_audio_budget_denied")

    recognizer = Denied("synthetic fallback transcript")
    manager, session, socket = setup(recognizer)
    await manager._run_utterance(session, b"\x01\x00" * 16000, recognizer, None)
    assert recognizer.calls == 1
    assert [
        message["reason_code"] for message in socket.texts if message["type"] == "turn.failed"
    ] == ["synthetic_audio_budget_denied"]
    assert not any(message["type"] == "voice.transcript" for message in socket.texts)


@pytest.mark.parametrize("entry", ["device", "proactive"])
@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_audio_output_denial_stops_stream_and_preserves_reason(
    entry: str,
    late: bool,
    cleanup_fails: bool,
) -> None:
    error = BudgetDenied("synthetic_audio_budget_denied")
    first = Synthesizer(AudioStream(error, late=late, cleanup_fails=cleanup_fails))
    backup = Synthesizer(AudioStream(None))
    chain = TtsProviderChain([first, backup])
    manager, session, socket = setup(chain=chain)
    emitted: list[tuple[str, Any]] = []

    async def emit(event: str, payload: Any) -> None:
        emitted.append((event, payload))

    with pytest.raises(BudgetDenied) as captured:
        if entry == "device":
            await manager.stream_device_speech("synthetic speech", PrivacyLevel.L1, emit)
        else:
            await manager._send_proactive(session, "synthetic speech", PrivacyLevel.L1)
    assert captured.value is error and backup.calls == 0
    assert first.stream.closed == 1 and not chain._blocked_until
    assert not any(event == "pet.audio.end" for event, _ in emitted)
    assert not any(message["type"] == "voice.sentence.end" for message in socket.texts)


@pytest.mark.parametrize("entry", ["device", "proactive"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_selected_output_cancel_closes_stream_without_changing_cancellation(
    entry: str,
    cleanup_fails: bool,
) -> None:
    error = asyncio.CancelledError()
    source = Synthesizer(AudioStream(error, late=True, cleanup_fails=cleanup_fails))
    chain = TtsProviderChain([source])
    manager, session, _ = setup(chain=chain)

    async def emit(event: str, payload: Any) -> None:
        pass

    with pytest.raises(asyncio.CancelledError) as captured:
        if entry == "device":
            await manager.stream_device_speech("synthetic speech", PrivacyLevel.L1, emit)
        else:
            await manager._send_proactive(session, "synthetic speech", PrivacyLevel.L1)
    assert captured.value is error and source.stream.closed == 1
    assert not chain._blocked_until


@pytest.mark.parametrize("entry", ["device", "proactive"])
async def test_successful_audio_output_closes_owned_stream_after_last_chunk(entry: str) -> None:
    source = Synthesizer(AudioStream(None))
    manager, session, socket = setup(chain=TtsProviderChain([source]))
    emitted: list[tuple[str, Any]] = []

    async def emit(event: str, payload: Any) -> None:
        emitted.append((event, payload))

    if entry == "device":
        assert await manager.stream_device_speech("synthetic speech", PrivacyLevel.L1, emit)
        assert emitted[-1][0] == "pet.audio.end"
        assert sum(event == "pet.audio.chunk" for event, _ in emitted) == 1
    else:
        assert await manager._send_proactive(session, "synthetic speech", PrivacyLevel.L1)
        assert socket.texts[-1]["type"] == "voice.sentence.end" and len(socket.audio) == 1
    assert source.stream.closed == 1 and source.stream.reads == 2


async def test_output_size_rejection_closes_stream_without_reading_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import voice_ws

    monkeypatch.setattr(voice_ws, "DEVICE_AUDIO_MAX_BYTES", 1)
    source = Synthesizer(AudioStream(None))
    chain = TtsProviderChain([source])
    manager, _, _ = setup(chain=chain)
    emitted: list[tuple[str, Any]] = []

    async def emit(event: str, payload: Any) -> None:
        emitted.append((event, payload))

    assert not await manager.stream_device_speech("synthetic speech", PrivacyLevel.L1, emit)
    assert emitted[-1] == ("pet.audio.failed", {"reason_code": "tts_audio_too_large"})
    assert source.stream.closed == source.stream.reads == 1 and not chain._blocked_until
