"""Deterministic memory strength; relevance and truth are separate dimensions."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class MemoryTier(StrEnum):
    DORMANT = "dormant"
    LOW = "low"
    ORDINARY = "ordinary"
    MEDIUM = "medium"
    IMPORTANT = "important"
    CORE = "core"


TIERS = tuple(MemoryTier)
POLICY_VERSION = "memory-life-v1"
# 初始等级只允许从中级向上定级；更低等级仍由自然遗忘过程产生。
ALLOWED_INITIAL_TIERS = (MemoryTier.MEDIUM, MemoryTier.IMPORTANT, MemoryTier.CORE)
DECAY_DAYS = {
    MemoryTier.CORE: 180,
    MemoryTier.IMPORTANT: 90,
    MemoryTier.MEDIUM: 30,
    MemoryTier.ORDINARY: 60,
    MemoryTier.LOW: 180,
}


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def decay_due(tier: MemoryTier, anchor: datetime, importance: float) -> datetime | None:
    days = DECAY_DAYS.get(tier)
    multiplier = 4 if importance >= 0.8 else 2 if importance >= 0.4 else 1
    return aware(anchor) + timedelta(days=days * multiplier) if days else None


@dataclass(frozen=True)
class StrengthDecision:
    tier: MemoryTier
    reason: str
    next_decay_at: datetime | None


def clamp_initial_tier(value: str | None) -> MemoryTier:
    """显式定级（管理端）只做合法值钳制；非法或缺省落回中级。"""
    if value is None:
        return MemoryTier.MEDIUM
    try:
        tier = MemoryTier(value)
    except ValueError:
        return MemoryTier.MEDIUM
    return tier if tier in ALLOWED_INITIAL_TIERS else MemoryTier.MEDIUM


def resolve_ai_initial_tier(
    value: str | None,
    *,
    importance: float,
    confidence: float | None,
    core_gate: float = 0.8,
    important_gate: float = 0.6,
) -> MemoryTier:
    """AI 初始等级围栏：只允许向上定级，并按置信度/重要性门禁降级。

    core 需要置信度与重要性都过 core_gate（默认 0.8），important 过
    important_gate（默认 0.6）；未提供置信度视为不达标（宁保守）。
    阈值可经调用方从环境注入校准（B3），本函数保持纯函数不读环境。
    结果只可能是 medium/important/core。
    """
    tier = clamp_initial_tier(value)
    if tier is MemoryTier.MEDIUM:
        return tier
    threshold = core_gate if tier is MemoryTier.CORE else important_gate
    if importance < threshold or confidence is None or confidence < threshold:
        return MemoryTier.MEDIUM
    return tier


_BIRTHDAY_DATE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_YEARLY_DATE = re.compile(r"每年(\d{1,2})月(\d{1,2})日")
# 2月29日生日按平年处理：周年落在3月1日（保守可唤醒，不静默错过）。
_DEFAULT_WAKE_AHEAD_DAYS = 7


def _next_anniversary(month: int, day: int, now: datetime) -> datetime | None:
    for year in (now.year, now.year + 1):
        candidate: datetime | None
        try:
            candidate = datetime(year, month, day, tzinfo=UTC)
        except ValueError:
            candidate = datetime(year, 3, 1, tzinfo=UTC) if (month, day) == (2, 29) else None
        if candidate is not None and candidate >= now:
            return candidate
    return None


def periodic_wake_at(
    content: str,
    fact_key: str | None,
    *,
    now: datetime,
    ahead_days: int = _DEFAULT_WAKE_AHEAD_DAYS,
) -> datetime | None:
    """周期事实（生日/每年纪念日）的下一次临近唤醒时间（B4）。

    生日经 fact_key 限定（*.birthday），纪念日经"每年M月D日"字面匹配；
    找不到可解析日期返回 None（不排程）。唤醒点 = 下一个周年 - ahead_days，
    已落入窗口内则取 now（下轮扫描立即唤醒，不静默错过当年）。
    """
    monthly_day: tuple[int, int] | None = None
    if fact_key and fact_key.endswith(".birthday"):
        match = _BIRTHDAY_DATE.search(content)
        if match:
            monthly_day = (int(match.group(2)), int(match.group(3)))
    else:
        match = _YEARLY_DATE.search(content)
        if match:
            monthly_day = (int(match.group(1)), int(match.group(2)))
    if monthly_day is None:
        return None
    anniversary = _next_anniversary(monthly_day[0], monthly_day[1], aware(now))
    if anniversary is None:
        return None
    wake = anniversary - timedelta(days=max(0, ahead_days))
    return wake if wake > aware(now) else aware(now)


def evaluate(
    *,
    tier: MemoryTier,
    changed_at: datetime,
    created_at: datetime,
    last_used_at: datetime | None,
    importance: float,
    locked: bool,
    now: datetime,
    score30: int = 0,
    days30: int = 0,
    score90: int = 0,
    days90: int = 0,
    used: bool = False,
    eligible: bool = True,
    promote: bool = True,
    decay: bool = True,
) -> StrengthDecision:
    now = aware(now)
    anchor = max(aware(changed_at), aware(created_at), aware(last_used_at or created_at))
    due = decay_due(tier, anchor, importance)
    if locked or not eligible:
        return StrengthDecision(tier, "locked" if locked else "ineligible", None)
    target, reason = tier, "unchanged"
    if used and tier in (MemoryTier.DORMANT, MemoryTier.LOW, MemoryTier.ORDINARY):
        target, reason = MemoryTier.MEDIUM, "used_again"
    elif used and promote and tier == MemoryTier.MEDIUM and score30 >= 6 and days30 >= 3:
        target, reason = MemoryTier.IMPORTANT, "frequent_use"
    elif (
        used
        and promote
        and tier == MemoryTier.IMPORTANT
        and score90 >= 18
        and days90 >= 8
        and now - aware(changed_at) >= timedelta(days=14)
    ):
        target, reason = MemoryTier.CORE, "sustained_use"
    elif not used and decay and due is not None and now >= due:
        target, reason = TIERS[TIERS.index(tier) - 1], "unused"
    return StrengthDecision(
        target, reason, decay_due(target, now if used or target != tier else anchor, importance)
    )
