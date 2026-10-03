"""Pure proactive counting semantics shared by prechecks and acceptance."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CRITICAL_EVENTS = frozenset({"water_leak", "water_leak_detected", "safety.alarm"})
VISIBLE_DECISIONS = ("inform", "ask", "suggest", "escalate")


def daily_window(now: datetime, timezone: str) -> tuple[datetime, datetime]:
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("Asia/Shanghai")
    local_day = now.astimezone(zone).date()
    return (
        datetime.combine(local_day, time.min, zone).astimezone(UTC),
        datetime.combine(local_day + timedelta(days=1), time.min, zone).astimezone(UTC),
    )
