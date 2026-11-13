"""面向业主的中文可读日志格式（方案批次八）。

约定：
- 行首保留 ASCII 前缀（如 ``memory.persist``）供机器检索，正文用中文标签；
- 内容预览默认输出 ≤40 字（业主本机排查用），``ARIA_MEMORY_LOG_PREVIEW=0``
  可整体隐藏——分享日志截图前建议关闭，避免生活事实进入日志文件。
"""

from __future__ import annotations

import os

TIER_LABELS: dict[str, str] = {
    "core": "核心",
    "important": "重要",
    "medium": "中级",
    "ordinary": "一般",
    "low": "不重要",
    "dormant": "冷存档",
}

DECISION_LABELS: dict[str, str] = {
    "created": "新增",
    "supported": "佐证（已有记忆获得支持）",
    "conflict": "疑似冲突（已挂起待裁决）",
    "superseded": "已被替代",
}

TYPE_LABELS: dict[str, str] = {
    "semantic": "事实",
    "preference": "偏好",
    "commitment": "约定",
    "episodic": "经历",
    "emotional": "情感",
}

_PREVIEW_LIMIT = 40


def tier_label(tier: str | None) -> str:
    return TIER_LABELS.get(tier or "", tier or "未知")


def decision_label(decision: str | None) -> str:
    return DECISION_LABELS.get(decision or "", decision or "未知")


def type_label(memory_type: str | None) -> str:
    return TYPE_LABELS.get(memory_type or "", memory_type or "未知")


def content_preview(content: str | None, *, limit: int = _PREVIEW_LIMIT) -> str:
    """内容预览；开关 ``ARIA_MEMORY_LOG_PREVIEW=0`` 时输出占位符（空内容不受开关影响）。"""
    text = (content or "").strip()
    if not text:
        return "（空）"
    if os.getenv("ARIA_MEMORY_LOG_PREVIEW", "1") != "1":
        return "（预览已隐藏）"
    return text if len(text) <= limit else f"{text[:limit]}…"
