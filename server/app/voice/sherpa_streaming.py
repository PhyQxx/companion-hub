"""sherpa-onnx 流式 ASR（docs/04 §6.4 P1）：本地流式 Zipformer 转写。

延迟加载，缺依赖/模型时不阻断 Hub 启动（与 faster-whisper 同策略）。
说话期间逐帧 feed（CPU 同步推理经调用方 to_thread），断句时 finalize
给出终稿并复位流；transcribe 整段路径保留作为降级（重放 PCM 到新流）。

模型目录需包含 encoder/decoder/joiner ONNX 与 tokens.txt（双语 int8
流式 Zipformer，约 70MB）；获取方式见 scripts/download_sherpa_model.sh。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import struct
from pathlib import Path
from typing import Any

from .contracts import SpeechRecognitionUnavailable

logger = logging.getLogger(__name__)

# finalize 前补喂的静音尾垫：让流式解码器把残留在窗内的音频吐完
_TAIL_SILENCE_SECONDS = 0.3


def _pcm16_to_floats(pcm: bytes) -> list[float]:
    count = len(pcm) // 2
    return [value / 32768.0 for value in struct.unpack(f"<{count}h", pcm[: count * 2])]


class SherpaStreamingRecognizer:
    """本地 sherpa-onnx 流式转写器；模型与流均首次使用时才加载。"""

    runs_local = True

    def __init__(self, *, model_dir: str, num_threads: int = 2) -> None:
        self._model_dir = model_dir
        self._num_threads = num_threads
        self._recognizer: Any | None = None
        self._stream: Any | None = None
        self._load_lock = asyncio.Lock()

    # ---- StreamingSpeechRecognizer ----

    def feed(self, pcm: bytes) -> str | None:
        """喂入一帧 PCM16；返回新的部分转写（无更新返回 None）。"""

        recognizer, stream = self._ensure_ready()
        if stream is None:
            return None
        recognizer.accept_waveform(stream, _pcm16_to_floats(pcm))
        return self._drain(recognizer, stream)

    def finalize(self) -> str:
        """补静音收尾并返回终稿；内部流复位供下一话语复用。"""

        recognizer, stream = self._ensure_ready()
        if stream is None:
            return ""
        recognizer.accept_waveform(
            stream, [0.0] * int(16000 * _TAIL_SILENCE_SECONDS)
        )
        recognizer.input_finished(stream)
        transcript = self._drain(recognizer, stream) or ""
        self._stream = None
        return transcript.strip()

    # ---- SpeechRecognizer（降级路径：整段重放） ----

    async def transcribe(
        self, pcm: bytes, *, sample_rate: int, language: str | None
    ) -> str:
        """整段转写降级路径：重放 PCM 到一个全新流（streaming 失败时兜底）。"""

        def _replay() -> str:
            # 重放用全新流，不携带上一话语的解码状态
            self._stream = None
            recognizer, stream = self._ensure_ready_sync()
            if stream is None:
                raise SpeechRecognitionUnavailable("sherpa_model_load_failed")
            recognizer.accept_waveform(stream, _pcm16_to_floats(pcm))
            recognizer.input_finished(stream)
            return (self._drain(recognizer, stream) or "").strip()

        return await asyncio.to_thread(_replay)

    # ---- 内部 ----

    def _drain(self, recognizer: Any, stream: Any) -> str | None:
        while recognizer.is_ready(stream):
            recognizer.decode(stream)
        result = recognizer.get_result(stream)
        return result if result else None

    def _ensure_ready(self) -> tuple[Any, Any]:
        """同步确保 recognizer/stream 就绪；供已在线程池内的调用方使用。"""

        if self._recognizer is None or self._stream is None:
            self._ensure_ready_sync()
        assert self._recognizer is not None
        return self._recognizer, self._stream

    def _ensure_ready_sync(self) -> tuple[Any, Any]:
        if self._recognizer is None:
            self._recognizer = _load_recognizer(self._model_dir, self._num_threads)
        if self._stream is None:
            self._stream = self._recognizer.create_stream()
        return self._recognizer, self._stream

    async def warmup(self) -> None:
        """把模型加载前移到 voice.hello，避免污染首回合的 finalize 延迟。"""

        if self._recognizer is None:
            async with self._load_lock:
                if self._recognizer is None:
                    self._recognizer = await asyncio.to_thread(
                        _load_recognizer, self._model_dir, self._num_threads
                    )


def _load_recognizer(model_dir: str, num_threads: int) -> Any:
    directory = Path(model_dir).expanduser()
    if not directory.is_dir():
        raise SpeechRecognitionUnavailable(
            "sherpa_model_not_configured"
        )
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
        logger.info(
            "loading sherpa-onnx streaming model dir=%s threads=%s",
            directory,
            num_threads,
        )
        return module.OnlineRecognizer.from_transducer(
            tokens=_find("tokens.txt"),
            encoder=_find("encoder-*.onnx"),
            decoder=_find("decoder-*.onnx"),
            joiner=_find("joiner-*.onnx"),
            num_threads=num_threads,
            sample_rate=16000,
            feature_dim=80,
        )
    except SpeechRecognitionUnavailable:
        raise
    except Exception as error:
        logger.warning(
            "sherpa-onnx model load failed dir=%s error_type=%s",
            directory,
            type(error).__name__,
        )
        raise SpeechRecognitionUnavailable("model_load_failed") from error
