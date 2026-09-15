from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.schemas.common import PrivacyLevel

from .models import (
    HistoryRecallResult,
    RecallMode,
    RecallPlan,
    TemporalRange,
    TimelineSearchResult,
    TimelineSourceType,
)
from .store import TimelineStore

_HISTORY_MARKERS = re.compile(
    r"(刚才|之前|以前|昨天|昨晚|前几天|上周|上周末|这个月|上个月|去年|那次|上次|曾经)"
)

_RECENT_DURATION = re.compile(
    r"(?:过去|最近)\s*([0-9一二两三四五六七八九十]{1,3})\s*(分钟|小时|天|周)"
)
_CN_NUMERAL_DIGITS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def _parse_recent_amount(text: str) -> int:
    """解析时间窗数量：阿拉伯数字或一~九十九的中文数字（含"两"和"十X/X十"）。"""
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if text.startswith("十"):
        return 10 + _CN_NUMERAL_DIGITS[text[1]]
    if "十" in text:
        tens, _, ones = text.partition("十")
        return _CN_NUMERAL_DIGITS[tens] * 10 + (
            _CN_NUMERAL_DIGITS[ones] if ones else 0
        )
    return _CN_NUMERAL_DIGITS[text]


def has_history_intent(query: str) -> bool:
    return bool(_HISTORY_MARKERS.search(query))


class TemporalQueryParser:
    def __init__(self, timezone_name: str = "Asia/Shanghai") -> None:
        try:
            self._timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            self._timezone = ZoneInfo("Asia/Shanghai")

    @property
    def timezone_name(self) -> str:
        return self._timezone.key

    def parse(self, query: str, *, now: datetime | None = None) -> TemporalRange | None:
        moment = (now or datetime.now(UTC)).astimezone(self._timezone)
        today = moment.date()

        recent = _RECENT_DURATION.search(query)
        if recent is not None:
            amount = _parse_recent_amount(recent.group(1))
            unit = recent.group(2)
            delta = {
                "分钟": timedelta(minutes=amount),
                "小时": timedelta(hours=amount),
                "天": timedelta(days=amount),
                "周": timedelta(weeks=amount),
            }[unit]
            return TemporalRange(
                moment - delta,
                moment + timedelta(seconds=1),
                recent.group(0),
            )

        if "最近" in query:
            # 无明确时长的"最近"按最近一天处理：否则"总结下最近的浏览记录"
            # 这类高频问法解析不出时间窗，活动回顾整条链路静默失效。
            return TemporalRange(
                moment - timedelta(hours=24),
                moment + timedelta(seconds=1),
                "最近",
            )

        if "刚才" in query:
            return TemporalRange(
                moment - timedelta(minutes=45),
                moment + timedelta(seconds=1),
                "刚才",
            )
        if any(marker in query for marker in ("今天早上", "今天上午", "今早", "今上午")):
            return TemporalRange(
                _local(today, time(5), self._timezone),
                min(
                    _local(today, time(12), self._timezone),
                    moment + timedelta(seconds=1),
                ),
                "今天上午",
            )
        if "今天下午" in query or "今下午" in query:
            return TemporalRange(
                _local(today, time(12), self._timezone),
                min(
                    _local(today, time(18), self._timezone),
                    moment + timedelta(seconds=1),
                ),
                "今天下午",
            )
        if "今天晚上" in query or "今晚" in query:
            return TemporalRange(
                _local(today, time(18), self._timezone),
                min(
                    _local(today + timedelta(days=1), time(0), self._timezone),
                    moment + timedelta(seconds=1),
                ),
                "今天晚上",
            )
        if "今天" in query:
            return TemporalRange(
                _local(today, time(0), self._timezone),
                moment + timedelta(seconds=1),
                "今天",
            )
        if "昨晚" in query:
            yesterday = today - timedelta(days=1)
            return TemporalRange(
                _local(yesterday, time(18), self._timezone),
                _local(today, time(6), self._timezone),
                "昨晚",
            )
        if "昨天" in query:
            yesterday = today - timedelta(days=1)
            return TemporalRange(
                _local(yesterday, time(0), self._timezone),
                _local(today, time(0), self._timezone),
                "昨天",
            )
        if "前几天" in query:
            return TemporalRange(
                moment - timedelta(days=7),
                moment + timedelta(seconds=1),
                "前几天",
            )
        if "上周末" in query:
            this_monday = today - timedelta(days=today.weekday())
            saturday = this_monday - timedelta(days=2)
            return TemporalRange(
                _local(saturday, time(0), self._timezone),
                _local(this_monday, time(0), self._timezone),
                "上周末",
            )
        if "上周" in query:
            this_monday = today - timedelta(days=today.weekday())
            last_monday = this_monday - timedelta(days=7)
            return TemporalRange(
                _local(last_monday, time(0), self._timezone),
                _local(this_monday, time(0), self._timezone),
                "上周",
            )
        if "上个月" in query:
            current_month = today.replace(day=1)
            previous_month_end = current_month - timedelta(days=1)
            previous_month = previous_month_end.replace(day=1)
            return TemporalRange(
                _local(previous_month, time(0), self._timezone),
                _local(current_month, time(0), self._timezone),
                "上个月",
            )
        if "这个月" in query or "本月" in query:
            current_month = today.replace(day=1)
            return TemporalRange(
                _local(current_month, time(0), self._timezone),
                moment + timedelta(seconds=1),
                "这个月",
            )
        if "去年" in query:
            return TemporalRange(
                _local(today.replace(year=today.year - 1, month=1, day=1), time(0), self._timezone),
                _local(today.replace(month=1, day=1), time(0), self._timezone),
                "去年",
            )
        if re.search(r"(之前|以前|那次|上次|曾经)", query):
            return TemporalRange(
                moment - timedelta(days=30),
                moment + timedelta(seconds=1),
                "近期历史",
            )
        return None


class HistoryRecallService:
    def __init__(
        self,
        store: TimelineStore,
        *,
        timezone_name: str = "Asia/Shanghai",
    ) -> None:
        self._store = store
        self._temporal = TemporalQueryParser(timezone_name)

    @property
    def timezone_name(self) -> str:
        return self._temporal.timezone_name

    def plan(
        self,
        query: str,
        *,
        now: datetime | None = None,
        conversation_id: UUID | None = None,
        timezone_name: str | None = None,
    ) -> RecallPlan:
        parser = (
            self._temporal
            if timezone_name is None or timezone_name == self.timezone_name
            else TemporalQueryParser(timezone_name)
        )
        temporal = parser.parse(query, now=now)
        return RecallPlan(
            query=query,
            start_at=temporal.start_at if temporal else None,
            end_at=temporal.end_at if temporal else None,
            source_types=(TimelineSourceType.MESSAGE, TimelineSourceType.EVENT),
            conversation_id=conversation_id,
            max_results=8,
        )

    async def recall(
        self,
        query: str,
        *,
        user_id: UUID,
        privacy_level: PrivacyLevel,
        now: datetime | None = None,
        timezone_name: str | None = None,
    ) -> HistoryRecallResult:
        parser = (
            self._temporal
            if timezone_name is None or timezone_name == self.timezone_name
            else TemporalQueryParser(timezone_name)
        )
        plan = self.plan(query, now=now, timezone_name=parser.timezone_name)
        levels = (
            (PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2)
            if privacy_level is PrivacyLevel.L2
            else (PrivacyLevel.L0, PrivacyLevel.L1)
        )
        search_count = 1
        result = await self._store.search(
            user_id=user_id,
            query=query,
            start_at=plan.start_at,
            end_at=plan.end_at,
            source_types=plan.source_types,
            conversation_id=plan.conversation_id,
            privacy_levels=levels,
            limit=plan.max_results,
        )

        if (
            privacy_level is PrivacyLevel.L2
            and plan.start_at is not None
            and plan.end_at is not None
            and not _is_broad_phrase(query)
        ):
            # L2 Timeline 故意不复制正文。因此即使 L0/L1 里有词法候选,
            # L2 的明确小时间窗仍补一次 L2-only time search, 避免之前的
            # 普通问句把真正的私密 Source 挡住。补充结果只在当前 L2 回合
            # 本地展开, 不会进入 L0/L1 云端上下文。
            search_count = 2
            l2_result = await self._store.search(
                user_id=user_id,
                query="",
                start_at=plan.start_at,
                end_at=plan.end_at,
                source_types=plan.source_types,
                conversation_id=plan.conversation_id,
                privacy_levels=(PrivacyLevel.L2,),
                limit=min(plan.max_results, 6),
            )
            if l2_result.events:
                seen = {item.id for item in l2_result.events}
                merged = l2_result.events + tuple(
                    item for item in result.events if item.id not in seen
                )
                result = TimelineSearchResult(
                    events=merged[: plan.max_results],
                    candidate_count=result.candidate_count,
                )

        # Broad relative phrases are allowed one bounded expansion from 30 to 90 days.
        if not result.events and plan.start_at is not None and _is_broad_phrase(query):
            moment = (now or datetime.now(UTC)).astimezone(ZoneInfo(parser.timezone_name))
            search_count = 2
            plan = RecallPlan(
                query=plan.query,
                start_at=moment - timedelta(days=90),
                end_at=moment + timedelta(seconds=1),
                source_types=plan.source_types,
                max_results=plan.max_results,
            )
            result = await self._store.search(
                user_id=user_id,
                query=query,
                start_at=plan.start_at,
                end_at=plan.end_at,
                source_types=plan.source_types,
                conversation_id=plan.conversation_id,
                privacy_levels=levels,
                limit=plan.max_results,
            )

        if not result.events:
            return HistoryRecallResult(
                mode=RecallMode.NONE,
                plan=plan,
                events=(),
                evidence=(),
                search_count=search_count,
                candidate_count=result.candidate_count,
            )

        evidence = await self._store.expand_sources(
            result.events,
            user_id=user_id,
            privacy_level=privacy_level,
            limit=6,
        )
        return HistoryRecallResult(
            mode=RecallMode.SOURCE if evidence else RecallMode.TIMELINE,
            plan=plan,
            events=result.events,
            evidence=evidence,
            search_count=search_count,
            candidate_count=result.candidate_count,
        )

    @staticmethod
    def render_context(result: HistoryRecallResult) -> str:
        if result.mode is RecallMode.NONE:
            return (
                "【历史回溯】已按用户提供的历史线索检索，但没有找到足以确认答案的证据。"
                "不要猜测或补写过去发生过的内容；需要时请用户提供更具体的时间或主题线索。"
            )
        lines: list[str] = []
        if result.evidence:
            for evidence_item in result.evidence:
                stamp = evidence_item.occurred_at.isoformat()
                lines.append(f"- [{stamp}][{evidence_item.actor}] {evidence_item.text}")
        else:
            for event_item in result.events:
                stamp = event_item.occurred_at.isoformat()
                lines.append(f"- [{stamp}][{event_item.actor}] {event_item.summary}")
        return (
            "【历史回溯证据】以下内容来自受控 Timeline/Source 检索。"
            "只能依据这些证据回答过去发生过什么；没有覆盖到的细节不要补全。\n"
            + "\n".join(lines)
        )


def _local(day: object, clock: time, timezone: ZoneInfo) -> datetime:
    from datetime import date

    if not isinstance(day, date):
        raise TypeError("day must be date")
    return datetime.combine(day, clock, tzinfo=timezone)


def _is_broad_phrase(query: str) -> bool:
    return bool(re.search(r"(之前|以前|那次|上次|曾经)", query))
