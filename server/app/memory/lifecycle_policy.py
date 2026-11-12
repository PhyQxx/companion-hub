"""Deterministic memory strength; relevance and truth are separate dimensions."""

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
    value: str | None, *, importance: float, confidence: float | None
) -> MemoryTier:
    """AI 初始等级围栏：只允许向上定级，并按置信度/重要性门禁降级。

    core 需要置信度与重要性都 ≥0.8，important 需要 ≥0.6；未提供置信度
    视为不达标（宁保守）。结果只可能是 medium/important/core。
    """
    tier = clamp_initial_tier(value)
    if tier is MemoryTier.MEDIUM:
        return tier
    threshold = 0.8 if tier is MemoryTier.CORE else 0.6
    if importance < threshold or confidence is None or confidence < threshold:
        return MemoryTier.MEDIUM
    return tier


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
