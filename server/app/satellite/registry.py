"""卫星会话注册表与唤醒仲裁：进程内状态，确定性判定。

注册表是内存态——卫星掉线即消失，重连从 idle 重新开始；Hub 重启后
卫星通过 hello 重新注册，无需持久化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.schemas import PrivacyLevel

from .models import (
    InvalidSatelliteTransition,
    SatelliteEvent,
    SatelliteSessionView,
    SatelliteState,
    next_state,
)

DEFAULT_WAKE_WINDOW_SECONDS = 2.0


@dataclass(slots=True)
class SatelliteSession:
    device_id: UUID
    owner_user_id: UUID
    room_id: str
    max_privacy_level: PrivacyLevel = PrivacyLevel.L1
    continuous_timeout_seconds: float = 8
    state: SatelliteState = SatelliteState.IDLE
    wake_count: int = 0
    suppressed_count: int = 0
    last_seen_at: datetime | None = None
    last_wake_awarded_at: datetime | None = None
    follow_up_until: datetime | None = None

    def view(self) -> SatelliteSessionView:
        return SatelliteSessionView(
            device_id=self.device_id,
            owner_user_id=self.owner_user_id,
            room_id=self.room_id,
            max_privacy_level=self.max_privacy_level.value,
            continuous_timeout_seconds=self.continuous_timeout_seconds,
            state=self.state,
            wake_count=self.wake_count,
            suppressed_count=self.suppressed_count,
            last_seen_at=self.last_seen_at,
            follow_up_until=self.follow_up_until,
        )


@dataclass(frozen=True, slots=True)
class WakeDecision:
    winner: bool
    reason_code: str
    session_state: SatelliteState


@dataclass(slots=True)
class SatelliteRegistry:
    sessions: dict[UUID, SatelliteSession] = field(default_factory=dict)
    # 每个 owner 最近一次唤醒胜出时间：仲裁窗口内后续唤醒一律压制
    last_awake_per_owner: dict[UUID, datetime] = field(default_factory=dict)
    wake_window_seconds: float = DEFAULT_WAKE_WINDOW_SECONDS

    def register(
        self,
        *,
        device_id: UUID,
        owner_user_id: UUID,
        room_id: str,
        max_privacy_level: PrivacyLevel = PrivacyLevel.L1,
        continuous_timeout_seconds: float = 8,
    ) -> SatelliteSession:
        # 重连视为全新会话，从 idle 开始；不清零计数便于观测
        session = self.sessions.get(device_id)
        if session is None:
            session = SatelliteSession(
                device_id=device_id,
                owner_user_id=owner_user_id,
                room_id=room_id,
                max_privacy_level=max_privacy_level,
                continuous_timeout_seconds=continuous_timeout_seconds,
            )
            self.sessions[device_id] = session
        else:
            session.owner_user_id = owner_user_id
            session.room_id = room_id
            session.max_privacy_level = max_privacy_level
            session.continuous_timeout_seconds = continuous_timeout_seconds
            session.state = SatelliteState.IDLE
            session.follow_up_until = None
        session.last_seen_at = datetime.now(UTC)
        return session

    def unregister(self, device_id: UUID) -> None:
        self.sessions.pop(device_id, None)

    def get(self, device_id: UUID) -> SatelliteSession | None:
        return self.sessions.get(device_id)

    def list_for_owner(self, owner_user_id: UUID) -> list[SatelliteSessionView]:
        return [
            session.view()
            for session in self.sessions.values()
            if session.owner_user_id == owner_user_id
        ]

    def apply_event(self, device_id: UUID, event: SatelliteEvent) -> SatelliteState:
        session = self.sessions.get(device_id)
        if session is None:
            raise LookupError("satellite not registered")
        target = next_state(session.state, event)
        session.state = target
        if target is not SatelliteState.LISTENING:
            session.follow_up_until = None
        session.last_seen_at = datetime.now(UTC)
        return target

    def has_active_session(self, owner_user_id: UUID) -> bool:
        return any(
            session.state is not SatelliteState.IDLE
            for session in self.sessions.values()
            if session.owner_user_id == owner_user_id
        )

    def arbitrate_wake(
        self,
        *,
        device_id: UUID,
        owner_user_id: UUID,
        now: datetime | None = None,
    ) -> WakeDecision:
        """SAT-02 就近响应：同一 owner 任一时刻只允许一个唤醒会话。

        判定顺序（全部确定性）：
        1. 该 owner 已有进行中的会话 → 压制（session_active）；
        2. 仲裁窗口内的重复唤醒 → 压制（arbitration_window）；
        3. 否则本卫星胜出，进入 listening 并刷新窗口。
        """
        moment = now or datetime.now(UTC)
        self.expire_follow_ups(moment)
        session = self.sessions.get(device_id)
        if session is None:
            return WakeDecision(False, "not_registered", SatelliteState.IDLE)
        if self.has_active_session(owner_user_id):
            session.suppressed_count += 1
            return WakeDecision(False, "session_active", session.state)
        last_awake = self.last_awake_per_owner.get(owner_user_id)
        if last_awake is not None and moment - last_awake < timedelta(
            seconds=self.wake_window_seconds
        ):
            session.suppressed_count += 1
            return WakeDecision(False, "arbitration_window", session.state)
        try:
            target = self.apply_event(device_id, SatelliteEvent.WAKE_ACCEPTED)
        except InvalidSatelliteTransition:
            session.suppressed_count += 1
            return WakeDecision(False, "illegal_state", session.state)
        session.wake_count += 1
        session.last_wake_awarded_at = moment
        self.last_awake_per_owner[owner_user_id] = moment
        return WakeDecision(True, "wake_accepted", target)

    def expire_follow_ups(self, now: datetime | None = None) -> list[UUID]:
        moment = now or datetime.now(UTC)
        expired: list[UUID] = []
        for session in self.sessions.values():
            if session.follow_up_until is None or session.follow_up_until > moment:
                continue
            session.state = SatelliteState.IDLE
            session.follow_up_until = None
            session.last_seen_at = moment
            expired.append(session.device_id)
        return expired
