"""FOCUS-01 专注守护确定性分析核心。

红线（docs/00「默认只建议，不自动拦截应用」）：本模块只产出建议信号，
绝不拦截应用或注入键盘鼠标。所有判定基于屏幕观察的时间跨度与主题
相似度（bigram），建议文案用确定性模板——不含屏幕原始内容，只含
统计结论与建议，避免把屏幕私密内容写进主动通知。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

FocusSignalKind = Literal["long_work", "context_switching", "off_target"]

# 阈值（构造器可覆盖以便测试）
DEFAULT_LONG_WORK_MINUTES = 90
DEFAULT_SWITCH_WINDOW_MINUTES = 30
DEFAULT_SWITCH_COUNT = 5
# 提醒冷却：同会话同类型信号的最小重复间隔
DEFAULT_NUDGE_COOLDOWN_MINUTES = 20
# 主题相似度：与 screen_recall 的 bigram 口径一致
SIMILARITY_OFF_TARGET = 0.5


@dataclass(frozen=True, slots=True)
class FocusSignal:
    kind: FocusSignalKind
    message: str
    evidence_observations: int
    window_minutes: int
    # 面向 UI 的建议动作提示（只是建议）
    suggestion: str


@dataclass(frozen=True, slots=True)
class FocusObservation:
    """专注分析所需的屏幕观察最小形状（来自 Timeline screen.observed）。"""

    occurred_at: datetime
    summary: str


@dataclass(frozen=True, slots=True)
class FocusSession:
    session_id: str
    user_id: str
    target: str
    target_keywords: tuple[str, ...]
    started_at: datetime
    ends_at: datetime
    # 每类信号上次提醒时间（冷却守卫）
    nudged_at: dict[str, datetime]

    @property
    def active(self) -> bool:
        return True


def _bigrams(text: str) -> set[str]:
    normalized = re.sub(r"\s+", "", text.lower())
    return {normalized[index : index + 2] for index in range(max(len(normalized) - 1, 0))}


def _similar(left: str, right: str) -> float:
    left_tokens = _bigrams(left)
    right_tokens = _bigrams(right)
    if not left_tokens or not right_tokens:
        return 1.0 if left.strip().lower() == right.strip().lower() else 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _topics(observations: list[FocusObservation]) -> list[str]:
    """把观察摘要聚合为"主题"：与相邻摘要相似度 ≥0.5 视为同一主题。"""
    topics: list[str] = []
    for observation in observations:
        summary = " ".join(observation.summary.split())
        if not summary:
            continue
        if topics and _similar(summary, topics[-1]) >= SIMILARITY_OFF_TARGET:
            continue
        topics.append(summary)
    return topics


def _matches_target(topic: str, keywords: tuple[str, ...]) -> bool:
    lowered = topic.lower()
    if any(keyword.lower() in lowered for keyword in keywords):
        return True
    return any(_similar(topic, keyword) >= SIMILARITY_OFF_TARGET for keyword in keywords)


def analyze_focus(
    observations: list[FocusObservation],
    *,
    now: datetime,
    session: FocusSession | None = None,
    long_work_minutes: int = DEFAULT_LONG_WORK_MINUTES,
    switch_window_minutes: int = DEFAULT_SWITCH_WINDOW_MINUTES,
    switch_count: int = DEFAULT_SWITCH_COUNT,
) -> tuple[FocusSignal, ...]:
    """从屏幕观察序列产出建议信号；观察按时间升序传入。"""
    signals: list[FocusSignal] = []
    relevant = [item for item in observations if item.occurred_at <= now and item.summary.strip()]
    if not relevant:
        return tuple(signals)

    # 1. 长时工作：观察时间跨度（首到尾）
    span = relevant[-1].occurred_at - relevant[0].occurred_at
    if span >= timedelta(minutes=long_work_minutes):
        hours = int(span.total_seconds() // 3600)
        minutes = int(span.total_seconds() % 3600 // 60)
        duration = f"{hours} 小时 {minutes} 分钟" if hours else f"{minutes} 分钟"
        signals.append(
            FocusSignal(
                kind="long_work",
                message=f"你已连续工作约 {duration}，建议起身休息 5 分钟，看看远处放松眼睛。",
                evidence_observations=len(relevant),
                window_minutes=int(span.total_seconds() // 60),
                suggestion="rest",
            )
        )

    # 2. 窗口切换：时间窗内不同主题数
    window_start = now - timedelta(minutes=switch_window_minutes)
    window = [item for item in relevant if item.occurred_at >= window_start]
    if len(window) >= 2:
        topics = _topics(window)
        if len(topics) >= switch_count:
            signals.append(
                FocusSignal(
                    kind="context_switching",
                    message=(
                        f"过去 {switch_window_minutes} 分钟屏幕主题切换了 {len(topics)} 次，"
                        "比跳来跳去更省力的是先做完手头这一件。"
                    ),
                    evidence_observations=len(window),
                    window_minutes=switch_window_minutes,
                    suggestion="prioritize",
                )
            )

    # 3. 偏离目标：会话有目标关键词，且近期主题全部与目标无关
    if session is not None and session.target_keywords:
        recent = [item for item in relevant if item.occurred_at >= window_start]
        if recent:
            topics = _topics(recent)
            if topics and not any(
                _matches_target(topic, session.target_keywords) for topic in topics
            ):
                signals.append(
                    FocusSignal(
                        kind="off_target",
                        message=(
                            f"专注目标「{session.target}」进行中：最近的屏幕活动似乎与目标无关，"
                            "建议回到手头的事。"
                        ),
                        evidence_observations=len(recent),
                        window_minutes=switch_window_minutes,
                        suggestion="refocus",
                    )
                )
    return tuple(signals)


def cooldown_passed(
    session: FocusSession,
    signal: FocusSignal,
    *,
    now: datetime,
    cooldown: timedelta = timedelta(minutes=DEFAULT_NUDGE_COOLDOWN_MINUTES),
) -> bool:
    last = session.nudged_at.get(signal.kind)
    return last is None or now - last >= cooldown


def with_nudge_marked(session: FocusSession, signal: FocusSignal, *, now: datetime) -> FocusSession:
    """返回标记了本次提醒时间的新会话（不可变更新）。"""
    nudged_at = dict(session.nudged_at)
    nudged_at[signal.kind] = now
    return FocusSession(
        session_id=session.session_id,
        user_id=session.user_id,
        target=session.target,
        target_keywords=session.target_keywords,
        started_at=session.started_at,
        ends_at=session.ends_at,
        nudged_at=nudged_at,
    )


__all__ = [
    "DEFAULT_LONG_WORK_MINUTES",
    "DEFAULT_NUDGE_COOLDOWN_MINUTES",
    "DEFAULT_SWITCH_COUNT",
    "DEFAULT_SWITCH_WINDOW_MINUTES",
    "FocusObservation",
    "FocusSession",
    "FocusSignal",
    "analyze_focus",
    "cooldown_passed",
    "with_nudge_marked",
]
