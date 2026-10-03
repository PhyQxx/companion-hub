"""sherpa-onnx 流式识别器：fake 模块下的 feed/finalize/降级与守卫语义。"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any, ClassVar

import pytest

from app.voice.contracts import SpeechRecognitionUnavailable
from app.voice.sherpa_streaming import SherpaStreamingRecognizer, _pcm16_to_floats


class FakeStream:
    def __init__(self) -> None:
        self.accepted: list[list[float]] = []
        self.pending_decodes = 0
        self.finished = False
        self.result: str | None = None

    def accept_waveform(self, sample_rate: int, samples: list[float]) -> None:
        assert sample_rate > 0
        self.accepted.append(samples)
        self.pending_decodes += 2

    def input_finished(self) -> None:
        self.finished = True


class FakeOnlineRecognizer:
    instances: ClassVar[list[FakeOnlineRecognizer]] = []
    ctor_kwargs: ClassVar[dict[str, Any] | None] = None

    def __init__(self) -> None:
        self.streams: list[FakeStream] = []
        self.result_queue: list[str | None] = []
        FakeOnlineRecognizer.instances.append(self)

    @classmethod
    def from_transducer(cls, **kwargs: Any) -> FakeOnlineRecognizer:
        FakeOnlineRecognizer.ctor_kwargs = kwargs
        FakeOnlineRecognizer.instances = []
        return cls()

    def create_stream(self) -> FakeStream:
        stream = FakeStream()
        self.streams.append(stream)
        return stream

    def is_ready(self, stream: FakeStream) -> bool:
        return stream.pending_decodes > 0

    def decode_stream(self, stream: FakeStream) -> None:
        stream.pending_decodes -= 1

    def get_result(self, stream: FakeStream) -> str | None:
        if self.result_queue:
            return self.result_queue.pop(0)
        return stream.result


@pytest.fixture
def fake_sherpa(monkeypatch: pytest.MonkeyPatch) -> type[FakeOnlineRecognizer]:
    module = types.ModuleType("sherpa_onnx")
    module.OnlineRecognizer = FakeOnlineRecognizer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sherpa_onnx", module)
    return FakeOnlineRecognizer


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "sherpa-model"
    directory.mkdir()
    (directory / "tokens.txt").write_text("你 好", encoding="utf-8")
    (directory / "encoder-int8.onnx").write_bytes(b"e")
    (directory / "decoder-int8.onnx").write_bytes(b"d")
    (directory / "joiner-int8.onnx").write_bytes(b"j")
    return directory


def test_pcm16_conversion() -> None:
    values = _pcm16_to_floats(b"\x00\x00\x00\x80\x00\x80")
    assert values[0] == 0.0
    assert values[1] == pytest.approx(-32768 / 32768)
    assert values[2] == pytest.approx(-32768 / 32768)


def test_missing_module_raises_unavailable(
    monkeypatch: pytest.MonkeyPatch, model_dir: Path
) -> None:
    monkeypatch.setitem(sys.modules, "sherpa_onnx", None)
    recognizer = SherpaStreamingRecognizer(model_dir=str(model_dir))
    with pytest.raises(SpeechRecognitionUnavailable) as error:
        recognizer.feed(b"\x00\x00" * 480)
    assert error.value.reason == "sherpa_onnx_not_installed"


def test_missing_model_dir_raises_unavailable() -> None:
    recognizer = SherpaStreamingRecognizer(model_dir="/nonexistent/sherpa")
    with pytest.raises(SpeechRecognitionUnavailable) as error:
        recognizer.feed(b"\x00\x00" * 480)
    assert error.value.reason == "sherpa_model_not_configured"


def test_feed_emits_partials_and_finalize_resets_stream(
    fake_sherpa: type[FakeOnlineRecognizer], model_dir: Path
) -> None:
    recognizer = SherpaStreamingRecognizer(model_dir=str(model_dir))
    instance = fake_sherpa.instances[0] if fake_sherpa.instances else None
    # 首帧触发懒加载
    assert recognizer.feed(b"\x01\x00" * 480) is None
    assert fake_sherpa.ctor_kwargs is not None
    assert fake_sherpa.ctor_kwargs["sample_rate"] == 16000

    instance = fake_sherpa.instances[-1]
    instance.result_queue = ["你好", None]
    stream_before = instance.streams[-1]
    assert recognizer.feed(b"\x01\x00" * 480) == "你好"
    assert recognizer.feed(b"\x01\x00" * 480) is None
    # 喂入的样本被归一化到 [-1, 1]
    samples = instance.streams[0].accepted[0]
    assert all(-1.0 <= value <= 1.0 for value in samples)

    instance.result_queue = ["你好世界"]
    final = recognizer.finalize()
    assert final == "你好世界"
    assert stream_before.finished is True
    # finalize 后内部流复位：下一话语喂入会创建新流
    instance.result_queue = []
    recognizer.feed(b"\x01\x00" * 480)
    assert len(instance.streams) == 2


def test_transcribe_fallback_replays_full_utterance(
    fake_sherpa: type[FakeOnlineRecognizer], model_dir: Path
) -> None:
    recognizer = SherpaStreamingRecognizer(model_dir=str(model_dir))
    recognizer.feed(b"\x01\x00" * 480)
    instance = fake_sherpa.instances[-1]
    instance.result_queue = ["整段结果"]

    import asyncio

    transcript = asyncio.run(
        recognizer.transcribe(b"\x01\x00" * 960, sample_rate=16000, language=None)
    )
    assert transcript == "整段结果"
    replay_stream = instance.streams[-1]
    assert len(replay_stream.accepted) == 1
    assert len(replay_stream.accepted[0]) == 960
    assert replay_stream.finished is True


def test_missing_model_files_raise_unavailable(
    fake_sherpa: type[FakeOnlineRecognizer], tmp_path: Path
) -> None:
    directory = tmp_path / "incomplete"
    directory.mkdir()
    (directory / "tokens.txt").write_text("你", encoding="utf-8")
    recognizer = SherpaStreamingRecognizer(model_dir=str(directory))
    with pytest.raises(SpeechRecognitionUnavailable) as error:
        recognizer.feed(b"\x00\x00" * 480)
    assert error.value.reason == "sherpa_model_files_missing"


async def test_utterance_sessions_and_full_replay_have_independent_decoder_state(
    fake_sherpa: type[FakeOnlineRecognizer], model_dir: Path
) -> None:
    from app.api.voice_ws import UtteranceStreamer

    recognizer = SherpaStreamingRecognizer(model_dir=str(model_dir))
    recognizer.feed(b"\x01\x00" * 32)
    first, second = UtteranceStreamer(recognizer), UtteranceStreamer(recognizer)
    await first.feed(b"\x02\x00" * 32)
    await second.feed(b"\x03\x00" * 64)
    model = fake_sherpa.instances[-1]
    default_stream, first_stream, second_stream = model.streams
    first_stream.result, second_stream.result = "first utterance", "second utterance"
    assert await first.finalize() == "first utterance"
    await recognizer.transcribe(b"\x04\x00" * 48, sample_rate=8000, language=None)
    assert not second_stream.finished and not default_stream.finished
    assert await second.feed(b"\x05\x00" * 16) is not None
    assert model.streams[2] is second_stream
    assert await second.finalize() == "second utterance"
    assert default_stream.accepted == [[1 / 32768] * 32]
    assert first_stream.accepted[0] == [2 / 32768] * 32
    assert second_stream.accepted[:2] == [[3 / 32768] * 64, [5 / 32768] * 16]
    assert model.streams[3].accepted == [[4 / 32768] * 48]
    assert len(fake_sherpa.instances) == 1


async def test_parallel_warmup_and_first_frames_load_one_model(
    model_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import time

    from app.api.voice_ws import UtteranceStreamer

    calls = 0
    model = FakeOnlineRecognizer()

    def load(*_: object) -> FakeOnlineRecognizer:
        nonlocal calls
        calls += 1
        time.sleep(0.02)
        return model

    monkeypatch.setattr("app.voice.sherpa_streaming._load_recognizer", load)
    recognizer = SherpaStreamingRecognizer(model_dir=str(model_dir))
    sessions = [UtteranceStreamer(recognizer) for _ in range(12)]
    await asyncio.gather(recognizer.warmup(), *(item.feed(b"\x01\x00" * 32) for item in sessions))
    assert calls == 1 and len(model.streams) == 12
    assert all(stream.accepted == [[1 / 32768] * 32] for stream in model.streams)


async def test_cancelled_inference_cannot_contaminate_the_next_utterance(
    fake_sherpa: type[FakeOnlineRecognizer], model_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading

    from app.api.voice_ws import UtteranceStreamer

    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = FakeStream.accept_waveform

    def blocked(stream: FakeStream, sample_rate: int, samples: list[float]) -> None:
        if samples[0] == 1 / 32768:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(3)
        original(stream, sample_rate, samples)

    monkeypatch.setattr(FakeStream, "accept_waveform", blocked)
    recognizer = SherpaStreamingRecognizer(model_dir=str(model_dir))
    old, current = UtteranceStreamer(recognizer), UtteranceStreamer(recognizer)
    task = asyncio.create_task(old.feed(b"\x01\x00" * 32))
    next_task: asyncio.Task[str | None] | None = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not old.active
        next_task = asyncio.create_task(current.feed(b"\x02\x00" * 48))
        release.set()
        await asyncio.wait_for(next_task, 2)
        model = fake_sherpa.instances[-1]
        assert len(model.streams) == 2
        assert model.streams[0].accepted == [[1 / 32768] * 32]
        assert model.streams[1].accepted == [[2 / 32768] * 48]
        model.streams[1].result = "current utterance"
        assert await current.finalize() == "current utterance"
        assert await old.finalize() is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if next_task is not None:
            if not next_task.done():
                next_task.cancel()
            await asyncio.gather(next_task, return_exceptions=True)
        await recognizer.warmup()  # Drain synchronous work before restoring fake bindings.
