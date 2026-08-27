"""TTS 提供方链：主备并存，一家失败自动切下一家（docs/33）。

- 逐句选择：select() 依次尝试候选，只有产出首个音频分片的提供方才被
  选定（首块前的失败对上层完全透明），保持流式首字延迟；
- 失败冷却：预首块失败或中途断流的提供方在冷却窗口内被跳过；
- L2 只允许链中的本地提供方（runs_local=True），没有则整体降级文字；
- mime / sample_rate 取自实际选中的提供方，客户端逐句自适应格式。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.schemas import PrivacyLevel

from .contracts import LocalOnlySynthesizerError, SpeechSynthesizer

logger = logging.getLogger(__name__)

FAILURE_COOLDOWN_S = 60.0


@dataclass(frozen=True, slots=True)
class TtsSelection:
    """一次句级选择结果：提供方 + 已取出的首块 + 未耗尽的后续流。"""

    provider: SpeechSynthesizer
    first_chunk: bytes
    stream: AsyncIterator[bytes]


class TtsProviderChain:
    """按顺序尝试多个合成器；失败冷却 + L2 本地过滤。"""

    def __init__(
        self,
        providers: list[SpeechSynthesizer],
        *,
        cooldown_s: float = FAILURE_COOLDOWN_S,
    ) -> None:
        if not providers:
            raise ValueError("tts chain requires at least one provider")
        self._providers = providers
        self._cooldown_s = cooldown_s
        self._blocked_until: dict[int, float] = {}

    @property
    def providers(self) -> tuple[SpeechSynthesizer, ...]:
        return tuple(self._providers)

    async def select(self, text: str, *, privacy_level: PrivacyLevel) -> TtsSelection:
        candidates = self._candidates(privacy_level)
        if not candidates:
            raise LocalOnlySynthesizerError(privacy_level)
        errors: list[str] = []
        for provider in candidates:
            stream = provider.synthesize(text, privacy_level=privacy_level)
            try:
                first_chunk = await stream.__anext__()
            except StopAsyncIteration:
                self.report_failure(provider)
                errors.append(f"{type(provider).__name__}:empty_stream")
                continue
            except LocalOnlySynthesizerError:
                raise
            except Exception as error:
                # 首块前失败：标记冷却并换下一家，上层无感知
                self.report_failure(provider)
                errors.append(f"{type(provider).__name__}:{type(error).__name__}")
                logger.warning(
                    "tts provider failed before first chunk provider=%s error=%s",
                    type(provider).__name__,
                    error,
                )
                continue
            return TtsSelection(provider=provider, first_chunk=first_chunk, stream=stream)
        raise RuntimeError(f"all tts providers failed: {', '.join(errors)}")

    def report_failure(self, provider: SpeechSynthesizer) -> None:
        """中途断流时由调用侧上报，冷却窗口内跳过该提供方。"""
        self._blocked_until[id(provider)] = time.monotonic() + self._cooldown_s

    def _candidates(self, privacy_level: PrivacyLevel) -> list[SpeechSynthesizer]:
        now = time.monotonic()
        allowed = [
            provider
            for provider in self._providers
            if privacy_level is not PrivacyLevel.L2 or provider.runs_local
        ]
        fresh = [
            provider
            for provider in allowed
            if self._blocked_until.get(id(provider), 0.0) <= now
        ]
        # 冷却把候选清空时退回全量：聊胜于无
        return fresh or allowed
