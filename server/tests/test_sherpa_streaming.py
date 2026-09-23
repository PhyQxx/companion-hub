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

    def accept_waveform(self, stream: FakeStream, samples: list[float]) -> None:
        stream.accepted.append(samples)
        # 模拟真实语义：新到音频需要若干轮 decode 才耗尽，is_ready 随之转 False
        stream.pending_decodes += 2

    def input_finished(self, stream: FakeStream) -> None:
        stream.finished = True

    def is_ready(self, stream: FakeStream) -> bool:
        return stream.pending_decodes > 0

    def decode(self, stream: FakeStream) -> None:
        stream.pending_decodes -= 1

    def get_result(self, stream: FakeStream) -> str | None:
        del stream
        if self.result_queue:
            return self.result_queue.pop(0)
        return None


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
