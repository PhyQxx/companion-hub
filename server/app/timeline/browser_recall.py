"""按时间窗聚合 browser.observed，供聊天回答"这段时间我看了哪些网站"。

与 screen_recall 同构：时间词法解析 → 时间线检索 → 段聚合 → 上下文渲染；
聚合键是站点 origin（比正文相似度更稳定的连续性信号）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.schemas.common import PrivacyLevel

from .models import TemporalRange, TimelineEvent, TimelineSourceType
from .recall import TemporalQueryParser
from .screen_recall import MERGE_GAP, _compact_segments, _similar
from .store import TimelineStore

BROWSER_EVENT_TYPE = "browser.observed"
MAX_BROWSER_EVENTS = 500
MAX_BROWSER_SEGMENTS = 24

_BROWSER_MARKERS = re.compile(r"(浏览|网页|网站|浏览器|上网)")
_SUMMARY_MARKERS = re.compile(
    r"(总结|概括|回顾|复盘|做了什么|干了什么|忙了什么|在忙什么|主要做什么|看了什么)"
)


def has_browser_activity_intent(query: str) -> bool:
    """只拦截浏览历史总结；查看"当前网页"仍交给 inspect_webpage 工具。"""
    return bool(_BROWSER_MARKERS.search(query) and _SUMMARY_MARKERS.search(query))


@dataclass(frozen=True, slots=True)
class BrowserActivitySegment:
    start_at: datetime
    end_at: datetime
    origin: str
    host: str
    page_titles: tuple[str, ...]
    summaries: tuple[str, ...]
    observation_count: int
    timeline_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class BrowserActivityRecallResult:
    temporal_range: TemporalRange
    events: tuple[TimelineEvent, ...]
    segments: tuple[BrowserActivitySegment, ...]
    candidate_count: int
    truncated: bool


class BrowserActivityRecallService:
    """按站点聚合周期浏览观察，与屏幕活动召回同一渲染形态。"""

    def __init__(
        self,
        store: TimelineStore,
        *,
        timezone_name: str = "Asia/Shanghai",
        max_events: int = MAX_BROWSER_EVENTS,
        max_segments: int = MAX_BROWSER_SEGMENTS,
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
    ) -> BrowserActivityRecallResult | None:
        if not has_browser_activity_intent(query):
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
            event_types=(BROWSER_EVENT_TYPE,),
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
        return BrowserActivityRecallResult(
            temporal_range=temporal_range,
            events=events,
            segments=segments,
            candidate_count=result.candidate_count,
            truncated=truncated,
        )

    @staticmethod
    def render_context(result: BrowserActivityRecallResult, *, timezone_name: str) -> str:
        try:
            timezone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            timezone = ZoneInfo("UTC")
        time_range = result.temporal_range
        header = (
            "【浏览活动回顾】以下内容来自指定时间范围内的周期浏览观察摘要。"
            "它是回答用户本次浏览/网站活动总结问题的主要证据。"
            "只能概括记录实际覆盖的内容，不要声称无法读取浏览记录；"
            "也不要把采样间隔当作精确的网站停留时长。\n"
            f"范围: {time_range.start_at.astimezone(timezone).isoformat(timespec='minutes')}"
            f" 至 {time_range.end_at.astimezone(timezone).isoformat(timespec='minutes')}"
            f" ({time_range.reason})"
        )
        if not result.segments:
            return (
                header
                + "\n该范围内没有浏览观察记录。请明确说明记录为空或感知未覆盖，"
                "不要用长期记忆猜测用户当时看了什么。"
            )
        lines: list[str] = []
        for segment in result.segments:
            start = segment.start_at.astimezone(timezone).strftime("%H:%M")
            end = segment.end_at.astimezone(timezone).strftime("%H:%M")
            summary = "；".join(segment.summaries)
            title = segment.page_titles[-1] if segment.page_titles else ""
            line = f"- [{start}-{end}][{segment.host}][{segment.observation_count} 次观察] "
            lines.append(f"{line}{title}：{summary}" if title else f"{line}{summary}")
        suffix = (
            "\n注意：结果达到读取上限，以下只覆盖时间范围内的部分记录。"
            if result.truncated
            else ""
        )
        return header + "\n" + "\n".join(lines) + suffix


def _aggregate(
    events: tuple[TimelineEvent, ...],
) -> tuple[BrowserActivitySegment, ...]:
    segments: list[BrowserActivitySegment] = []
    for event in events:
        origin = _origin(event)
        host = _host(origin)
        title = _title(event)
        summary = " ".join(event.summary.split())
        if not summary:
            continue
        if segments and _can_merge(segments[-1], event, origin):
            previous = segments[-1]
            summaries = previous.summaries
            if not any(_similar(summary, existing) >= 0.82 for existing in summaries):
                summaries = (*summaries, summary)
            titles = previous.page_titles
            if not title or title not in titles:
                titles = (*titles, title) if title else titles
            segments[-1] = BrowserActivitySegment(
                start_at=previous.start_at,
                end_at=event.occurred_at,
                origin=previous.origin,
                host=previous.host,
                page_titles=titles[-3:],
                summaries=summaries[-3:],
                observation_count=previous.observation_count + 1,
                timeline_ids=(*previous.timeline_ids, event.id),
            )
            continue
        segments.append(
            BrowserActivitySegment(
                start_at=event.occurred_at,
                end_at=event.occurred_at,
                origin=origin,
                host=host,
                page_titles=(title,) if title else (),
                summaries=(summary,),
                observation_count=1,
                timeline_ids=(event.id,),
            )
        )
    return tuple(segments)


def _can_merge(
    previous: BrowserActivitySegment,
    event: TimelineEvent,
    origin: str,
) -> bool:
    return previous.origin == origin and event.occurred_at - previous.end_at <= MERGE_GAP


def _origin(event: TimelineEvent) -> str:
    value = event.metadata.get("origin", "")
    return value.strip() if isinstance(value, str) else ""


def _host(origin: str) -> str:
    try:
        return (urlsplit(origin).hostname or "").casefold()
    except ValueError:
        return ""


def _title(event: TimelineEvent) -> str:
    value = event.metadata.get("page_title", "")
    return " ".join(value.split()) if isinstance(value, str) else ""
