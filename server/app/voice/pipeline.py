# ruff: noqa: RUF001, RUF002
"""句级切分：LLM 流式输出按强标点切句，首句立即送 TTS（docs/02 §3.2）。"""

from __future__ import annotations

SENTENCE_TERMINATORS = ("。", "！", "？", "!", "?", "\n")
MAX_SENTENCE_CHARS = 60


class SentenceBuffer:
    """累积流式 delta，遇到句末标点或达到长度上限时产出完整句子。"""

    def __init__(self, *, max_chars: int = MAX_SENTENCE_CHARS) -> None:
        self._pending = ""
        self._max_chars = max_chars

    def push(self, delta: str) -> list[str]:
        self._pending += delta
        sentences: list[str] = []
        while True:
            cut = min(
                (index for index in map(self._pending.find, SENTENCE_TERMINATORS) if index >= 0),
                default=-1,
            )
            if cut < 0:
                break
            sentence = self._pending[: cut + 1].strip()
            self._pending = self._pending[cut + 1 :]
            if sentence:
                sentences.append(sentence)
        while len(self._pending) > self._max_chars:
            sentences.append(self._pending[: self._max_chars])
            self._pending = self._pending[self._max_chars :]
        return sentences

    def flush(self) -> str:
        """回合结束时取走残余文本（无标点收尾的尾巴句）。"""
        remainder = self._pending.strip()
        self._pending = ""
        return remainder
