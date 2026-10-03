"""Local sherpa-onnx model with independent utterance decoder streams."""

from __future__ import annotations

import asyncio
import importlib
import logging
import struct
import threading
from pathlib import Path
from typing import Any

from .contracts import SpeechRecognitionUnavailable

logger = logging.getLogger(__name__)
_TAIL_SILENCE_SECONDS = 0.3
_SAMPLE_RATE = 16000


def _pcm16_to_floats(pcm: bytes) -> list[float]:
    count = len(pcm) // 2
    return [value / 32768.0 for value in struct.unpack(f"<{count}h", pcm[: count * 2])]


class SherpaStreamingRecognizer:
    """Share loaded model weights, never a decoder stream between utterances."""

    runs_local = True

    def __init__(self, *, model_dir: str, num_threads: int = 2) -> None:
        self._model_dir = model_dir
        self._num_threads = num_threads
        self._recognizer: Any | None = None
        self._model_lock = threading.RLock()
        # Compatibility for direct feed/finalize library calls. Production
        # UtteranceStreamer uses create_session instead of this default stream.
        self._default_session = self.create_session()

    def create_session(self) -> SherpaRecognitionSession:
        return SherpaRecognitionSession(self)

    def feed(self, pcm: bytes) -> str | None:
        return self._default_session.feed(pcm)

    def finalize(self) -> str:
        return self._default_session.finalize()

    async def transcribe(self, pcm: bytes, *, sample_rate: int, language: str | None) -> str:
        if sample_rate < 1:
            raise SpeechRecognitionUnavailable("invalid_sample_rate")
        # Full replay gets its own stream and cannot reset an active partial.
        return await asyncio.to_thread(self.create_session().replay, pcm, sample_rate)

    def _model(self) -> Any:
        # All callers run under the same synchronous lock, including warmup.
        # Cancelling to_thread cannot release this lock while inference runs.
        if self._recognizer is None:
            self._recognizer = _load_recognizer(self._model_dir, self._num_threads)
        return self._recognizer

    async def warmup(self) -> None:
        def load() -> None:
            with self._model_lock:
                self._model()

        await asyncio.to_thread(load)


class SherpaRecognitionSession:
    """One PCM decoder state, owned by one UtteranceStreamer."""

    runs_local = True

    def __init__(self, owner: SherpaStreamingRecognizer) -> None:
        self._owner = owner
        self._stream: Any | None = None

    def _ready(self) -> tuple[Any, Any]:
        recognizer = self._owner._model()
        if self._stream is None:
            self._stream = recognizer.create_stream()
        return recognizer, self._stream

    @staticmethod
    def _drain(recognizer: Any, stream: Any) -> str | None:
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        result = recognizer.get_result(stream)
        return result if isinstance(result, str) and result else None

    def feed(self, pcm: bytes) -> str | None:
        with self._owner._model_lock:
            recognizer, stream = self._ready()
            stream.accept_waveform(_SAMPLE_RATE, _pcm16_to_floats(pcm))
            return self._drain(recognizer, stream)

    def finalize(self) -> str:
        with self._owner._model_lock:
            if self._stream is None:
                return ""
            recognizer, stream = self._ready()
            try:
                stream.accept_waveform(
                    _SAMPLE_RATE, [0.0] * int(_SAMPLE_RATE * _TAIL_SILENCE_SECONDS)
                )
                stream.input_finished()
                return (self._drain(recognizer, stream) or "").strip()
            finally:
                self._stream = None

    def replay(self, pcm: bytes, sample_rate: int) -> str:
        with self._owner._model_lock:
            recognizer, stream = self._ready()
            try:
                stream.accept_waveform(sample_rate, _pcm16_to_floats(pcm))
                stream.input_finished()
                return (self._drain(recognizer, stream) or "").strip()
            finally:
                self._stream = None


def _load_recognizer(model_dir: str, num_threads: int) -> Any:
    directory = Path(model_dir).expanduser()
    if not directory.is_dir():
        raise SpeechRecognitionUnavailable("sherpa_model_not_configured")
    try:
        module = importlib.import_module("sherpa_onnx")
    except ModuleNotFoundError as error:
        raise SpeechRecognitionUnavailable("sherpa_onnx_not_installed") from error

    def _find(name: str) -> str:
        matches = sorted(directory.glob(name))
        if not matches:
            raise SpeechRecognitionUnavailable("sherpa_model_files_missing")
        return str(matches[0])

    try:
        logger.info("loading sherpa-onnx streaming model dir=%s threads=%s", directory, num_threads)
        return module.OnlineRecognizer.from_transducer(
            tokens=_find("tokens.txt"),
            encoder=_find("encoder-*.onnx"),
            decoder=_find("decoder-*.onnx"),
            joiner=_find("joiner-*.onnx"),
            num_threads=num_threads,
            sample_rate=_SAMPLE_RATE,
            feature_dim=80,
        )
    except SpeechRecognitionUnavailable:
        raise
    except Exception as error:
        logger.warning(
            "sherpa-onnx model load failed dir=%s error_type=%s", model_dir, type(error).__name__
        )
        raise SpeechRecognitionUnavailable("model_load_failed") from error
