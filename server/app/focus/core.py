"""FOCUS-01 专注会话服务：内存会话注册表 + 屏幕观察评估。

会话是短生命周期状态（小时级），与 SatelliteRegistry 同模式保存在
进程内存——重启丢失会话是可接受的降级，不引入新表。评估读取
Timeline 的 screen.observed 事件（L0/L1），异步评估由调度器驱动。
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.context.snapshots import timeline_reference
from app.focus.analysis import (
    DEFAULT_LONG_WORK_MINUTES,
    DEFAULT_SWITCH_COUNT,
    DEFAULT_SWITCH_WINDOW_MINUTES,
    FocusObservation,
    FocusSession,
    FocusSignal,
    analyze_focus,
)
from app.harness.context import ContextReference
from app.harness.time import utc
from app.schemas.common import PrivacyLevel
from app.timeline.ports import TimelineSearchRepository

MAX_SESSION_MINUTES = 480
MAX_KEYWORDS = 10


@dataclass(frozen=True)
class FocusEvaluation:
    signals: tuple[FocusSignal, ...]
    references: tuple[ContextReference, ...]


class FocusSessionService:
    def __init__(
        self,
        timeline_store: TimelineSearchRepository,
        *,
        long_work_minutes: int = DEFAULT_LONG_WORK_MINUTES,
        switch_window_minutes: int = DEFAULT_SWITCH_WINDOW_MINUTES,
        switch_count: int = DEFAULT_SWITCH_COUNT,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._timeline = timeline_store
        self._long_work = long_work_minutes
        self._switch_window = switch_window_minutes
        self._switch_count = switch_count
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sessions: dict[str, FocusSession] = {}

    def start_session(
        self,
        user_id: str,
        *,
        target: str,
        keywords: list[str],
        duration_minutes: int,
    ) -> FocusSession:
        now = self._clock()
        cleaned = [keyword.strip() for keyword in keywords if keyword.strip()][:MAX_KEYWORDS]
        if not cleaned:
            cleaned = [target.strip()]
        # 一人同时只有一个专注会话：新会话替换旧会话
        session = FocusSession(
            session_id=secrets.token_hex(8),
            user_id=user_id,
            target=target.strip(),
            target_keywords=tuple(cleaned),
            started_at=now,
            ends_at=now + timedelta(minutes=duration_minutes),
            nudged_at={},
        )
        self._sessions[user_id] = session
        return session

    def stop_session(self, user_id: str) -> FocusSession | None:
        return self._sessions.pop(user_id, None)

    def get_session(self, user_id: str) -> FocusSession | None:
        session = self._sessions.get(user_id)
        if session is None:
            return None
        if self._clock() >= session.ends_at:
            # 到期会话顺带清理
            self._sessions.pop(user_id, None)
            return None
        return session

    def active_sessions(self) -> list[FocusSession]:
        now = self._clock()
        expired = [key for key, session in self._sessions.items() if now >= session.ends_at]
        for key in expired:
            self._sessions.pop(key, None)
        return list(self._sessions.values())

    def mark_nudged(self, user_id: str, signal: FocusSignal, *, now: datetime) -> None:
        from app.focus.analysis import with_nudge_marked

        session = self._sessions.get(user_id)
        if session is not None:
            self._sessions[user_id] = with_nudge_marked(session, signal, now=now)

    async def evaluate(
        self,
        session: FocusSession,
        *,
        now: datetime | None = None,
    ) -> tuple[FocusSignal, ...]:
        return (await self.evaluate_snapshot(session, now=now)).signals

    async def evaluate_snapshot(
        self,
        session: FocusSession,
        *,
        now: datetime | None = None,
    ) -> FocusEvaluation:
        """Capture the exact owned timeline rows used by deterministic analysis."""
        moment = utc(now or self._clock())
        # 观察窗：固定回看上限（长时工作跨度需要会话开始前的整段序列）
        window_start = moment - timedelta(minutes=MAX_SESSION_MINUTES)
        result = await self._timeline.search(
            user_id=UUID(session.user_id),
            start_at=window_start,
            end_at=moment,
            event_types=("screen.observed",),
            privacy_levels=(PrivacyLevel.L0, PrivacyLevel.L1),
            limit=200,
            candidate_limit=201,
        )
        events = tuple(
            event
            for event in result.events
            if event.user_id == UUID(session.user_id)
            and event.privacy_level in ("L0", "L1")
            and event.event_type == "screen.observed"
            and window_start <= utc(event.occurred_at) <= moment
            and event.summary.strip()
        )
        observations = sorted(
            (
                FocusObservation(occurred_at=utc(event.occurred_at), summary=event.summary)
                for event in events
            ),
            key=lambda item: item.occurred_at,
        )
        signals = analyze_focus(
            observations,
            now=moment,
            session=session,
            long_work_minutes=self._long_work,
            switch_window_minutes=self._switch_window,
            switch_count=self._switch_count,
        )
        return FocusEvaluation(signals, tuple(timeline_reference(event) for event in events))


__all__ = ["MAX_SESSION_MINUTES", "FocusEvaluation", "FocusSessionService"]
