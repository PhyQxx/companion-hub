"""把展示用 Markdown 转成适合语音合成的纯文本。"""

from __future__ import annotations

import html
import re

_FENCE = re.compile(r"(```|~~~)")
_LINK_TARGET = r"\((?:[^()]|\([^()]*\))*\)"
_IMAGE = re.compile(rf"!\[([^]]*)]{_LINK_TARGET}")
_LINK = re.compile(rf"\[([^]]+)]{_LINK_TARGET}")
_AUTOLINK = re.compile(r"<https?://[^>]+>", re.IGNORECASE)
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_HTML_TAG = re.compile(r"</?[a-z][^>]*>", re.IGNORECASE)
_INLINE_CODE = re.compile(r"(`+)(.*?)\1", re.DOTALL)
_LINE_MARKER = re.compile(
    r"(?m)^\s{0,3}(?:#{1,6}\s+|>\s*|[-+*]\s+|\d+[.)]\s+|[-*_]{3,}\s*$)"
)
_TASK_MARKER = re.compile(r"(?i)\[[ x]]\s+")
_TABLE_RULE = re.compile(r"(?m)^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")


class MarkdownSpeechFilter:
    """增量移除 Markdown；跨句记住 fenced code block 状态。"""

    def __init__(self) -> None:
        self._inside_fence = False

    def clean(self, text: str) -> str:
        visible: list[str] = []
        for part in _FENCE.split(text):
            if part in {"```", "~~~"}:
                self._inside_fence = not self._inside_fence
            elif not self._inside_fence:
                visible.append(part)
        return _clean_inline("".join(visible))


def markdown_to_speech_text(text: str) -> str:
    """一次性清理 Markdown 文本，供主动播报和设备播报使用。"""
    return MarkdownSpeechFilter().clean(text)


def _clean_inline(text: str) -> str:
    text = _IMAGE.sub(lambda match: match.group(1), text)
    text = _LINK.sub(lambda match: match.group(1), text)
    text = _AUTOLINK.sub("", text)
    text = _URL.sub("", text)
    text = _HTML_TAG.sub("", text)
    text = _INLINE_CODE.sub(lambda match: match.group(2), text)
    text = _TABLE_RULE.sub("", text)
    text = _LINE_MARKER.sub("", text)
    text = _TASK_MARKER.sub("", text)
    text = re.sub(r"[*_~]", "", text)
    # URL、文件路径和 Markdown 转义中常见的斜杠不应被 TTS 逐字念出。
    text = text.replace("/", "、").replace("\\", "、")
    text = text.replace("|", "，")
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"(^|\s)、+", r"\1", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"[、，]\s*[、，]+", "，", text)
    return text.strip(" \t\r\n、，")
