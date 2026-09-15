"""COMMUTE-01 出行管家：下一个带地点的日程 → 路线耗时 → 建议出发时刻。

红线（docs/00「建议来源和时间可解释」）：路线耗时来自高德路线规划的
真实响应，缓冲分钟数来自配置，建议出发时刻 = 日程开始 - 路线耗时 -
缓冲；三项来源都随结果透出。日程取消/改期时经 `commute:{event_id}`
source_ref 复用 TASK-01 调度底座联动撤旧，不引入新调度器。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from app.calendar import CalendarEventView
from app.schemas.common import PrivacyLevel
from app.tasks.models import TaskKind, TaskTrigger
from app.tasks.store import TaskStore

DEPARTURE_SOURCE_PREFIX = "commute:"
DEFAULT_WITHIN_HOURS = 24
MAX_BUFFER_FALLBACK_SECONDS = 3 * 3600




@dataclass(frozen=True, slots=True)
class CommutePlan:
    event_id: str
    event_title: str
    starts_at: datetime
    origin_text: str
    destination_text: str
    mode: str
    distance_m: int | None
    duration_s: int | None
    leave_by: datetime
    navigation_uri: str | None
    weather_summary: str | None
    # 可解释性：每项结论的来源
    duration_source: str
    reminder_task_id: str | None

    def to_payload(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_title": self.event_title,
            "starts_at": self.starts_at.isoformat(),
            "origin": self.origin_text,
            "destination": self.destination_text,
            "mode": self.mode,
            "distance_m": self.distance_m,
            "duration_s": self.duration_s,
            "leave_by": self.leave_by.isoformat(),
            "leave_by_local": self.leave_by.strftime("%H:%M"),
            "duration_source": self.duration_source,
            "weather_summary": self.weather_summary,
            "navigation_uri": self.navigation_uri,
            "reminder_task_id": self.reminder_task_id,
        }


class CommuteAmapProvider(Protocol):
    """出行服务用到的高德能力子集（复用 tools.amap.AmapProvider 实例）。"""

    async def geocode(self, address: str, *, city: str | None = None) -> dict[str, object]: ...

    async def route(
        self,
        origin: str,
        destination: str,
        *,
        mode: str,
        origin_citycode: str | None = None,
        destination_citycode: str | None = None,
    ) -> dict[str, object]: ...


class CommuteRouteError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class CommuteCalendarStore(Protocol):
    """出行服务用到的日历能力子集（CalendarStore 满足）。"""

    async def list_events(
        self,
        user_id: UUID,
        *,
        starts_from: datetime | None = None,
        starts_to: datetime | None = None,
        include_cancelled: bool = False,
        limit: int = 200,
    ) -> list[CalendarEventView]: ...


class CommuteService:
    def __init__(
        self,
        calendar_store: CommuteCalendarStore,
        task_store: TaskStore,
        amap: CommuteAmapProvider,
        *,
        origin: str,
        mode: str = "driving",
        buffer_minutes: int = 10,
        default_city: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._calendar = calendar_store
        self._tasks = task_store
        self._amap = amap
        self._origin = origin.strip()
        self._mode = mode
        self._buffer = timedelta(minutes=buffer_minutes)
        self._city = default_city
        self._clock = clock or (lambda: datetime.now(UTC))

    async def next_outing(
        self,
        user_id: UUID,
        *,
        within_hours: int = DEFAULT_WITHIN_HOURS,
    ) -> CalendarEventView | None:
        """时间窗内下一个带地点的日程；无则返回 None。"""
        now = self._clock()
        events = await self._calendar.list_events(
            user_id,
            starts_from=now,
            starts_to=now + timedelta(hours=within_hours),
        )
        for event in events:  # list_events 按 starts_at 升序
            if (event.location or "").strip():
                return event
        return None

    async def plan_commute(
        self,
        user_id: UUID,
        event: CalendarEventView,
        *,
        reminder: bool = True,
    ) -> CommutePlan:
        """路线耗时 → 建议出发时刻；可选设置出发提醒（撤旧建新）。"""
        destination_text = (event.location or "").strip()
        now = self._clock()
        starts_at = event.starts_at
        if starts_at.tzinfo is None:
            starts_at = starts_at.replace(tzinfo=UTC)

        try:
            origin_geo = await self._amap.geocode(self._origin, city=self._city)
        except CommuteRouteError:
            raise
        except Exception as error:
            raise CommuteRouteError("origin_geocode_failed") from error
        origin_coord = str(origin_geo.get("location") or "")
        if not origin_coord:
            raise CommuteRouteError("origin_geocode_failed")
        try:
            destination_geo = await self._amap.geocode(destination_text, city=self._city)
        except CommuteRouteError:
            raise
        except Exception as error:
            raise CommuteRouteError("destination_geocode_failed") from error
        destination_coord = str(destination_geo.get("location") or "")
        if not destination_coord:
            raise CommuteRouteError("destination_geocode_failed")

        try:
            payload = await self._amap.route(
                origin_coord,
                destination_coord,
                mode=self._mode,
                origin_citycode=str(origin_geo.get("citycode") or "") or None,
                destination_citycode=str(destination_geo.get("citycode") or "") or None,
            )
            from app.tools.nearby import parse_route

            distance, duration, _steps = parse_route(payload, self._mode)
        except CommuteRouteError:
            raise
        except Exception as error:
            raise CommuteRouteError("route_failed") from error
        if duration is None:
            raise CommuteRouteError("route_duration_missing")
        duration_source = f"amap:{self._mode}:{duration}s"
        leave_by = starts_at - timedelta(seconds=duration) - self._buffer
        if leave_by < now:
            # 出发时刻已过：按"尽快出发"处理，仍如实保留计算值
            leave_by = now + timedelta(seconds=1)

        weather_summary = await self._destination_weather(destination_geo)

        navigation_uri = _navigation_uri(
            origin_coord, destination_coord, destination_text, self._mode
        )

        reminder_task_id: str | None = None
        if reminder:
            reminder_task_id = await self._sync_reminder(
                user_id,
                event_id=str(event.id),
                event_title=event.title,
                leave_by=leave_by,
                starts_at=starts_at,
                now=now,
            )

        return CommutePlan(
            event_id=str(event.id),
            event_title=event.title,
            starts_at=starts_at,
            origin_text=self._origin,
            destination_text=str(
                destination_geo.get("formatted_address") or destination_text
            ),
            mode=self._mode,
            distance_m=distance,
            duration_s=duration,
            leave_by=leave_by,
            navigation_uri=navigation_uri,
            weather_summary=weather_summary,
            duration_source=duration_source,
            reminder_task_id=reminder_task_id,
        )

    async def _destination_weather(self, destination_geo: dict[str, object]) -> str | None:
        """目的地所在城市的实时天气；geocode 未返回 adcode 时静默降级。"""
        adcode = str(destination_geo.get("adcode") or "")
        weather = getattr(self._amap, "weather", None)
        if not adcode or weather is None:
            return None
        try:
            payload = await weather(adcode, extensions="base")
        except Exception:
            return None
        forecasts = payload.get("lives") if isinstance(payload, dict) else None
        if not isinstance(forecasts, list) or not forecasts or not isinstance(forecasts[0], dict):
            return None
        live = forecasts[0]
        condition = str(live.get("weather") or "")
        temperature = str(live.get("temperature") or "")
        if not condition:
            return None
        return f"{condition} {temperature}°C".strip()

    async def _sync_reminder(
        self,
        user_id: UUID,
        *,
        event_id: str,
        event_title: str,
        leave_by: datetime,
        starts_at: datetime,
        now: datetime,
    ) -> str | None:
        """出发提醒经 TASK-01 调度，source_ref=commute:{event_id} 撤旧建新。"""
        ref = f"{DEPARTURE_SOURCE_PREFIX}{event_id}"
        await self._tasks.cancel_tasks_by_source_ref(user_id, ref)
        task = await self._tasks.create(
            user_id=user_id,
            kind=TaskKind.REMINDER,
            title=f"该出发了：{event_title}（{starts_at.strftime('%H:%M')} 开始）",
            trigger=TaskTrigger(type="time", at=leave_by),
            privacy_level=PrivacyLevel.L1,
            source="commute",
            source_ref=ref,
            now=now,
        )
        return str(task.id)

    async def aclose(self) -> None:
        close = getattr(self._amap, "close", None)
        if close is not None:
            await close()

    async def cancel_reminder(self, user_id: UUID, event_id: str) -> None:
        await self._tasks.cancel_tasks_by_source_ref(
            user_id, f"{DEPARTURE_SOURCE_PREFIX}{event_id}"
        )


def _navigation_uri(
    origin: str, destination: str, name: str, mode: str
) -> str:
    """高德导航 URI Scheme（与 RouteTool 同形状，供客户端一键导航）。"""
    from urllib.parse import quote

    return (
        "https://uri.amap.com/navigation?"
        f"from={origin}&to={destination}&via=&mode={mode}"
        f"&policy=&src=&coordinate=gaode&callnative=0&name={quote(name)}"
    )


__all__ = [
    "DEFAULT_WITHIN_HOURS",
    "CommutePlan",
    "CommuteRouteError",
    "CommuteService",
]
