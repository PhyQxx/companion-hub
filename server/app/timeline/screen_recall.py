from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.schemas.common import PrivacyLevel

from .models import TemporalRange, TimelineEvent, TimelineSourceType
from .recall import TemporalQueryParser
from .store import TimelineStore

SCREEN_EVENT_TYPE = "screen.observed"
MAX_SCREEN_EVENTS = 500
MAX_SEGMENTS = 24
MERGE_GAP = timedelta(minutes=10)

_SCREEN_MARKERS = re.compile(r"(屏幕|电脑|桌面|显示器|电脑上|电脑里)")
_SUMMARY_MARKERS = re.compile(
    r"(总结|概括|回顾|复盘|做了什么|干了什么|忙了什么|在忙什么|主要做什么|看了什么)"
)


def has_screen_activity_intent(query: str) -> bool:
    """只拦截屏幕历史总结；查看“当前屏幕”仍交给 capture_screen 工具。"""
    return bool(_SCREEN_MARKERS.search(query) and _SUMMARY_MARKERS.search(query))


@dataclass(frozen=True, slots=True)
class ScreenActivitySegment:
    start_at: datetime
    end_at: datetime
    display: int
    summaries: tuple[str, ...]
    observation_count: int
    timeline_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ScreenActivityRecallResult:
    temporal_range: TemporalRange
    events: tuple[TimelineEvent, ...]
    segments: tuple[ScreenActivitySegment, ...]
    candidate_count: int
    truncated: bool


class ScreenActivityRecallService:
    """按时间聚合周期屏幕观察，供聊天回答一段时间内的电脑活动。"""

    def __init__(
        self,
        store: TimelineStore,
        *,
        timezone_name: str = "Asia/Shanghai",
        max_events: int = MAX_SCREEN_EVENTS,
        max_segments: int = MAX_SEGMENTS,
    ) -> None:
        self._store = store
        self._temporal = TemporalQueryParser(timezone_name)
        self._max_events = max_events
        self._max_segments = max_segments

    async def recall(
        self,
        query: str,
        *,
        user_id: UUID,
        privacy_level: PrivacyLevel,
        now: datetime,
        timezone_name: str,
    ) -> ScreenActivityRecallResult | None:
        if not has_screen_activity_intent(query):
            return None
        parser = (
            self._temporal
            if timezone_name == self._temporal.timezone_name
            else TemporalQueryParser(timezone_name)
        )
        temporal_range = parser.parse(query, now=now)
        if temporal_range is None:
            return None
        privacy_levels = (
            (PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2)
            if privacy_level is PrivacyLevel.L2
            else (PrivacyLevel.L0, PrivacyLevel.L1)
        )
        # 时间总结必须取时间窗内事件，不能拿整句问题做词法相关性过滤。
        result = await self._store.search(
            user_id=user_id,
            query="",
            start_at=temporal_range.start_at,
            end_at=temporal_range.end_at,
            source_types=(TimelineSourceType.DEVICE,),
            event_types=(SCREEN_EVENT_TYPE,),
            privacy_levels=privacy_levels,
            limit=self._max_events,
            candidate_limit=self._max_events + 1,
        )
        events = tuple(sorted(result.events, key=lambda item: item.occurred_at))
        truncated = result.candidate_count > self._max_events
        if truncated:
            events = events[: self._max_events]
        segments = _aggregate(events)
        if len(segments) > self._max_segments:
            segments = _compact_segments(segments, self._max_segments)
        return ScreenActivityRecallResult(
            temporal_range=temporal_range,
            events=events,
            segments=segments,
            candidate_count=result.candidate_count,
            truncated=truncated,
        )

    @staticmethod
    def render_context(result: ScreenActivityRecallResult, *, timezone_name: str) -> str:
        try:
            timezone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            timezone = ZoneInfo("UTC")
        time_range = result.temporal_range
        header = (
            "【屏幕活动回顾】以下内容来自指定时间范围内的周期屏幕观察摘要。"
            "它是回答用户本次电脑活动总结问题的主要证据。"
            "只能概括记录实际覆盖的内容，不要声称无法读取屏幕；"
            "也不要把采样间隔当作精确的应用使用时长。\n"
            f"范围: {time_range.start_at.astimezone(timezone).isoformat(timespec='minutes')}"
            f" 至 {time_range.end_at.astimezone(timezone).isoformat(timespec='minutes')}"
            f" ({time_range.reason})"
        )
        if not result.segments:
            return (
                header
                + "\n该范围内没有屏幕观察记录。请明确说明记录为空或感知未覆盖，"
                "不要用长期记忆猜测用户当时做了什么。"
            )
        lines: list[str] = []
        for segment in result.segments:
            start = segment.start_at.astimezone(timezone).strftime("%H:%M")
            end = segment.end_at.astimezone(timezone).strftime("%H:%M")
            summary = "；".join(segment.summaries)
            lines.append(
                f"- [{start}-{end}][显示器 {segment.display}]"
                f"[{segment.observation_count} 次观察] {summary}"
            )
        suffix = (
            "\n注意：结果达到读取上限，以下只覆盖时间范围内的部分记录。"
            if result.truncated
            else ""
        )
        return header + "\n" + "\n".join(lines) + suffix


def _aggregate(events: tuple[TimelineEvent, ...]) -> tuple[ScreenActivitySegment, ...]:
    segments: list[ScreenActivitySegment] = []
    for event in events:
        display = _display(event)
        summary = " ".join(event.summary.split())
        if not summary:
            continue
        if segments and _can_merge(segments[-1], event, display, summary):
            previous = segments[-1]
            summaries = previous.summaries
            if not any(_similar(summary, existing) >= 0.82 for existing in summaries):
                summaries = (*summaries, summary)
            segments[-1] = ScreenActivitySegment(
                start_at=previous.start_at,
                end_at=event.occurred_at,
                display=display,
                summaries=summaries[-3:],
                observation_count=previous.observation_count + 1,
                timeline_ids=(*previous.timeline_ids, event.id),
            )
            continue
        segments.append(
            ScreenActivitySegment(
                start_at=event.occurred_at,
                end_at=event.occurred_at,
                display=display,
                summaries=(summary,),
                observation_count=1,
                timeline_ids=(event.id,),
            )
        )
    return tuple(segments)


def _can_merge(
    previous: ScreenActivitySegment,
    event: TimelineEvent,
    display: int,
    summary: str,
) -> bool:
    if previous.display != display or event.occurred_at - previous.end_at > MERGE_GAP:
        return False
    return max(_similar(summary, existing) for existing in previous.summaries) >= 0.5


def _similar(left: str, right: str) -> float:
    left_tokens = _bigrams(left)
    right_tokens = _bigrams(right)
    if not left_tokens or not right_tokens:
        return 1.0 if left.strip().lower() == right.strip().lower() else 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _bigrams(text: str) -> set[str]:
    normalized = re.sub(r"\s+", "", text.lower())
    return {normalized[index : index + 2] for index in range(max(len(normalized) - 1, 0))}


def _display(event: TimelineEvent) -> int:
    value = event.metadata.get("display", 0)
    if not isinstance(value, (int, float, str)):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _compact_segments(
    segments: tuple[ScreenActivitySegment, ...], limit: int
) -> tuple[ScreenActivitySegment, ...]:
    """保留时间分布，避免长时间窗只剩开头或结尾。"""
    if len(segments) <= limit:
        return segments
    if limit <= 1:
        return segments[:1]
    indexes = {
        round(index * (len(segments) - 1) / (limit - 1))
        for index in range(limit)
    }
    return tuple(segments[index] for index in sorted(indexes))
