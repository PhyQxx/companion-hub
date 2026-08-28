"""faster-whisper 本地 ASR：延迟加载，缺依赖时不阻断 Hub 启动。"""

from __future__ import annotations

import asyncio
import importlib
import logging
from io import BytesIO
from typing import Any

from .contracts import SpeechRecognitionUnavailable
from .mimo import wrap_wav

logger = logging.getLogger(__name__)

DEFAULT_INITIAL_PROMPT = "这是一段与智能伴侣小艾（Aria）的普通话对话。"
DEFAULT_HOTWORDS = "小艾 Aria 只回答"


class FasterWhisperRecognizer:
    """本地 faster-whisper 整段转写器；模型首次使用时才加载。"""

    runs_local = True

    def __init__(
        self,
        *,
        model: str = "small",
        device: str = "auto",
        compute_type: str = "default",
        language: str = "auto",
        initial_prompt: str | None = DEFAULT_INITIAL_PROMPT,
        hotwords: str | None = DEFAULT_HOTWORDS,
    ) -> None:
        self._model_name = model
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._initial_prompt = initial_prompt
        self._hotwords = hotwords
        self._model: Any | None = None
        self._load_lock = asyncio.Lock()

    async def transcribe(
        self, pcm: bytes, *, sample_rate: int, language: str | None
    ) -> str:
        model = await self._ensure_model()
        wav = wrap_wav(pcm, sample_rate=sample_rate)
        selected_language = language or self._language
        requested_language = None if selected_language == "auto" else selected_language
        try:
            return await asyncio.to_thread(
                self._transcribe_sync,
                model,
                wav,
                requested_language,
                self._initial_prompt,
                self._hotwords,
            )
        except SpeechRecognitionUnavailable:
            raise
        except Exception as error:
            logger.warning(
                "faster-whisper transcription failed model=%s error_type=%s",
                self._model_name,
                type(error).__name__,
            )
            raise SpeechRecognitionUnavailable("transcription_failed") from error

    async def warmup(self) -> None:
        """在开始录音前加载本地模型。

        首次下载仍由管理端环境自检/人工操作触发；这里只把已缓存模型的
        进程级初始化前移到 voice.hello，避免污染“说完→首音频”指标。
        """

        await self._ensure_model()

    async def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._load_model_sync)
        return self._model

    def _load_model_sync(self) -> Any:
        try:
            module = importlib.import_module("faster_whisper")
            whisper_model = module.WhisperModel
        except ModuleNotFoundError as error:
            raise SpeechRecognitionUnavailable("faster_whisper_not_installed") from error
        try:
            logger.info(
                "loading faster-whisper model=%s device=%s compute_type=%s",
                self._model_name,
                self._device,
                self._compute_type,
            )
            return whisper_model(
                self._model_name,
                device=self._device,
                compute_type=self._compute_type,
            )
        except Exception as error:
            logger.warning(
                "faster-whisper model load failed model=%s error_type=%s",
                self._model_name,
                type(error).__name__,
            )
            raise SpeechRecognitionUnavailable("model_load_failed") from error

    @staticmethod
    def _transcribe_sync(
        model: Any,
        wav: bytes,
        language: str | None,
        initial_prompt: str | None,
        hotwords: str | None,
    ) -> str:
        segments, _info = model.transcribe(
            BytesIO(wav),
            language=language,
            beam_size=1,
            vad_filter=False,
            initial_prompt=initial_prompt,
            hotwords=hotwords,
        )
        return "".join(str(segment.text) for segment in segments).strip()
