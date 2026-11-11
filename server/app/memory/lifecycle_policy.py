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
