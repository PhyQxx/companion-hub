# ruff: noqa: RUF001, RUF002, RUF003
"""语音管线单元测试：VAD 断句、句级切分、WAV 包装、TTS 故障转移、MiMo 契约。"""

from __future__ import annotations

import base64
import json
import wave
from collections.abc import AsyncIterator
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.schemas import PrivacyLevel
from app.voice import (
    EnergyVad,
    FasterWhisperRecognizer,
    MiMoAsrRecognizer,
    MiMoTtsSynthesizer,
    SentenceBuffer,
    TtsProviderChain,
    pcm16_rms,
    wrap_wav,
)
from app.voice.contracts import LocalOnlySynthesizerError


def loud_frames(count: int) -> bytes:
    # 恒定幅度 8000 的 PCM16 帧（30ms/帧），RMS=8000 远高于阈值 550
    return b"\x40\x1f" * (480 * count)


def silent_frames(count: int) -> bytes:
    return b"\x00\x00" * (480 * count)


def test_pcm16_rms_matches_known_constant_amplitude() -> None:
    assert pcm16_rms(b"") == 0
    assert pcm16_rms(silent_frames(1)) == 0
    assert pcm16_rms(loud_frames(1)) == 8000
    assert pcm16_rms(b"\x40\x1f\xff") == 8000


async def test_faster_whisper_adapter_lazy_loads_and_joins_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeWhisperModel:
        def __init__(self, model: str, *, device: str, compute_type: str) -> None:
            captured.update(model=model, device=device, compute_type=compute_type)

        def transcribe(
            self,
            audio: BytesIO,
            *,
            language: str | None,
            beam_size: int,
            vad_filter: bool,
        ) -> tuple[list[SimpleNamespace], object]:
            captured.update(
                wav_header=audio.read(4),
                language=language,
                beam_size=beam_size,
                vad_filter=vad_filter,
            )
            return [SimpleNamespace(text="你好"), SimpleNamespace(text="，世界")], object()

    monkeypatch.setattr(
        "app.voice.faster_whisper.importlib.import_module",
        lambda name: SimpleNamespace(WhisperModel=FakeWhisperModel),
    )
    recognizer = FasterWhisperRecognizer(
        model="small", device="cpu", compute_type="int8", language="zh"
    )

    text = await recognizer.transcribe(loud_frames(5), sample_rate=16_000, language=None)

    assert text == "你好，世界"
    assert captured == {
        "model": "small",
        "device": "cpu",
        "compute_type": "int8",
        "wav_header": b"RIFF",
        "language": "zh",
        "beam_size": 1,
        "vad_filter": False,
    }


def test_energy_vad_segments_utterance_boundaries() -> None:
    vad = EnergyVad()
    events = []

    # 2 帧响帧不足以触发开始（需连续 3 帧）
    events.append(vad.feed(loud_frames(2)))
    assert all(event is None for event in events)

    started = vad.feed(loud_frames(2))
    assert started is not None and started.kind == "utterance_started"

    # 静音 hangover：14 帧静音仍处于说话中，第 15 帧判结束
    for _ in range(14):
        assert vad.feed(silent_frames(1)) is None
    ended = vad.feed(silent_frames(1))
    assert ended is not None and ended.kind == "utterance_ended"


def test_energy_vad_force_end_for_ptt() -> None:
    vad = EnergyVad()
    assert vad.feed(loud_frames(4)) is not None
    event = vad.force_end()
    assert event is not None and event.kind == "utterance_ended"
    assert vad.force_end() is None


def test_sentence_buffer_splits_on_terminators_and_length_cap() -> None:
    buffer = SentenceBuffer()
    assert buffer.push("你好呀。今天") == ["你好呀。"]
    assert buffer.push("天气") == []
    assert buffer.push("不错！") == ["今天天气不错！"]
    # 无标点长句按 60 字上限强制切分
    long_text = "数" * 130
    sentences = buffer.push(long_text)
    assert [len(sentence) for sentence in sentences] == [60, 60]
    assert buffer.flush() == "数" * 10


def test_wrap_wav_roundtrips_pcm() -> None:
    pcm = loud_frames(5)
    wrapped = wrap_wav(pcm, sample_rate=16_000)
    with wave.open(BytesIO(wrapped)) as reader:
        assert reader.getframerate() == 16_000
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.readframes(reader.getnframes()) == pcm


class FakeSynthesizer:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        runs_local: bool = False,
        fail_before_first: bool = False,
        fail_mid_stream: bool = False,
    ) -> None:
        self.runs_local = runs_local
        self.mime = "audio/pcm;rate=24000"
        self.sample_rate = 24_000
        self._chunks = chunks
        self._fail_before_first = fail_before_first
        self._fail_mid_stream = fail_mid_stream

    async def synthesize(
        self, text: str, *, privacy_level: PrivacyLevel
    ) -> AsyncIterator[bytes]:
        if privacy_level is PrivacyLevel.L2 and not self.runs_local:
            raise LocalOnlySynthesizerError(privacy_level)
        if self._fail_before_first:
            raise RuntimeError("provider down")
        for index, chunk in enumerate(self._chunks):
            if index == 1 and self._fail_mid_stream:
                raise RuntimeError("stream broke")
            yield chunk


async def test_tts_chain_falls_back_when_primary_fails_before_first_chunk() -> None:
    primary = FakeSynthesizer([b"AA"], fail_before_first=True)
    backup = FakeSynthesizer([b"BB", b"CC"])
    chain = TtsProviderChain([primary, backup])

    selection = await chain.select("你好", privacy_level=PrivacyLevel.L1)

    assert selection.provider is backup
    assert selection.first_chunk == b"BB"
    collected = [selection.first_chunk]
    async for chunk in selection.stream:
        collected.append(chunk)
    assert collected == [b"BB", b"CC"]


async def test_tts_chain_treats_empty_stream_as_failure() -> None:
    primary = FakeSynthesizer([])
    backup = FakeSynthesizer([b"OK"])
    chain = TtsProviderChain([primary, backup])

    selection = await chain.select("你好", privacy_level=PrivacyLevel.L1)

    assert selection.provider is backup


async def test_tts_chain_requires_local_provider_for_l2() -> None:
    chain = TtsProviderChain([FakeSynthesizer([b"AA"])])

    with pytest.raises(LocalOnlySynthesizerError):
        await chain.select("私密内容", privacy_level=PrivacyLevel.L2)

    local = FakeSynthesizer([b"LL"], runs_local=True)
    chain_with_local = TtsProviderChain([FakeSynthesizer([b"AA"]), local])
    selection = await chain_with_local.select("私密内容", privacy_level=PrivacyLevel.L2)
    assert selection.provider is local


async def test_tts_chain_skips_reported_failure_within_cooldown() -> None:
    primary = FakeSynthesizer([b"AA"])
    backup = FakeSynthesizer([b"BB"])
    chain = TtsProviderChain([primary, backup])
    chain.report_failure(primary)

    selection = await chain.select("你好", privacy_level=PrivacyLevel.L1)

    assert selection.provider is backup


async def test_mimo_asr_sends_wav_data_url_and_parses_text() -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "今天天气怎么样"}}]}
        )

    pcm = loud_frames(10)
    recognizer = MiMoAsrRecognizer(
        "test-key", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    text = await recognizer.transcribe(pcm, sample_rate=16_000, language="zh")

    assert text == "今天天气怎么样"
    assert captured["url"].endswith("/chat/completions")
    assert captured["auth"] == "Bearer test-key"
    body = captured["json"]
    assert body["model"] == "mimo-v2.5-asr"
    assert body["asr_options"] == {"language": "zh"}
    data_url = body["messages"][0]["content"][0]["input_audio"]["data"]
    assert data_url.startswith("data:audio/wav;base64,")
    with wave.open(BytesIO(base64.b64decode(data_url.split(",", 1)[1]))) as reader:
        assert reader.readframes(reader.getnframes()) == pcm


async def test_mimo_tts_streams_base64_pcm_chunks() -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        first = base64.b64encode(b"\x01\x00\x02\x00").decode()
        second = base64.b64encode(b"\x03\x00").decode()
        sse = (
            f'data: {{"choices":[{{"delta":{{"audio":{{"data":"{first}"}}}}}}]}}\n\n'
            f'data: {{"choices":[{{"delta":{{"audio":{{"data":"{second}"}}}}}}]}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    synthesizer = MiMoTtsSynthesizer(
        "test-key", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    chunks = [
        chunk
        async for chunk in synthesizer.synthesize("你好呀", privacy_level=PrivacyLevel.L1)
    ]

    assert chunks == [b"\x01\x00\x02\x00", b"\x03\x00"]
    body = captured["json"]
    assert body["model"] == "mimo-v2.5-tts"
    assert body["audio"] == {"format": "pcm16", "voice": "冰糖"}
    assert body["messages"] == [{"role": "assistant", "content": "你好呀"}]
    assert body["stream"] is True


async def test_mimo_tts_rejects_l2_egress() -> None:
    synthesizer = MiMoTtsSynthesizer(
        "test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="data: [DONE]\n\n")
        )),
    )
    with pytest.raises(LocalOnlySynthesizerError):
        async for _ in synthesizer.synthesize("私密内容", privacy_level=PrivacyLevel.L2):
            pass
