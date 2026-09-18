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


def _normalize_for_echo(text: str) -> str:
    return "".join(character for character in text if character.isalnum())


def _looks_like_prompt_echo(text: str, prompt: str) -> bool:
    """噪声上 whisper 倾向把 initial_prompt "念"出来；高度重合即判幻听。

    双重条件：转写的大字块大多来自提示词（overlap），且覆盖提示词的大
    部分内容（coverage）。用户真实话语即使提到"智能伴侣小艾"，只会命中
    提示词的零碎片段，coverage 很低，不受影响。
    """
    normalized_text = _normalize_for_echo(text)
    normalized_prompt = _normalize_for_echo(prompt)
    if len(normalized_text) < 8 or len(normalized_prompt) < 8:
        return False
    text_bigrams = {
        normalized_text[index : index + 2]
        for index in range(len(normalized_text) - 1)
    }
    prompt_bigrams = {
        normalized_prompt[index : index + 2]
        for index in range(len(normalized_prompt) - 1)
    }
    shared = len(text_bigrams & prompt_bigrams)
    return (
        shared / max(1, len(text_bigrams)) >= 0.5
        and shared / max(1, len(prompt_bigrams)) >= 0.55
    )


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
            # 连续对话中麦克风常开，静音/噪声段会被 whisper 幻听成提示词
            # 或复读循环：内置 VAD 先剔除无语音段（纯噪声返回空转写，
            # 由调用方按空文本丢弃），关闭跨窗条件避免把上一窗的幻觉
            # 喂进下一窗滚雪球。
            vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt=initial_prompt,
            hotwords=hotwords,
        )
        pieces: list[str] = []
        for segment in segments:
            # whisper 自带阈值按整窗丢弃，这里逐段复核：疑似无语音的低
            # 置信段与高压缩比复读段整段跳过。
            no_speech = float(getattr(segment, "no_speech_prob", 0.0) or 0.0)
            avg_logprob = float(getattr(segment, "avg_logprob", 0.0) or 0.0)
            compression = float(getattr(segment, "compression_ratio", 1.0) or 1.0)
            if no_speech > 0.5 and avg_logprob < -0.9:
                continue
            text = str(segment.text)
            if compression > 2.5 and len(text.strip()) >= 8:
                continue
            pieces.append(text)
        transcript = "".join(pieces).strip()
        if transcript and initial_prompt and _looks_like_prompt_echo(transcript, initial_prompt):
            return ""
        return transcript
