from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from app.cognition import SemanticEvent
from app.cognition.proactive import CRITICAL_EVENTS as CRITICAL_EVENTS
from app.cognition.proactive import daily_window
from app.cognition.quota import proactive_count
from app.db import AppUserRecord, Database

from .models import ProactivePolicySettings


class ProactivePolicy:
    def __init__(
        self,
        database: Database,
        settings: ProactivePolicySettings | None = None,
    ) -> None:
        self._database = database
        self.settings = settings or ProactivePolicySettings()

    async def reject_reason(
        self,
        event: SemanticEvent,
        *,
        now: datetime,
    ) -> str | None:
        if event.expires_at is not None and event.expires_at <= now:
            return "event_expired"
        critical = event.kind in CRITICAL_EVENTS
        if bool(event.attributes.get("dnd", False)) and not critical:
            return "dnd"
        timezone = await self._timezone(event)
        local_now = now.astimezone(timezone)
        if self._in_quiet_hours(local_now.timetz().replace(tzinfo=None)) and not (
            critical and self.settings.critical_bypasses_quiet_hours
        ):
            return "quiet_hours"
        day_start, day_end = daily_window(now, timezone.key)
        async with self._database.sessions.begin() as session:
            sent_today = await proactive_count(session, event.user_id, day_start, day_end)
        if sent_today >= self.settings.daily_limit and not critical:
            return "daily_limit"
        return None

    async def _timezone(self, event: SemanticEvent) -> ZoneInfo:
        async with self._database.sessions.begin() as session:
            name = await session.scalar(
                select(AppUserRecord.timezone).where(AppUserRecord.id == event.user_id)
            )
        try:
            return ZoneInfo(name or "Asia/Shanghai")
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("Asia/Shanghai")

    def _in_quiet_hours(self, current: time) -> bool:
        start = time.fromisoformat(self.settings.quiet_hours_start)
        end = time.fromisoformat(self.settings.quiet_hours_end)
        if start == end:
            return False
        if start < end:
            return start <= current < end
        return current >= start or current < end
