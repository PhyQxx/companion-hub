"""Owned report collection from explicit DTO query ports, without SQL or SDKs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from hashlib import sha256
from uuid import UUID
from zoneinfo import ZoneInfo

from app.harness.budget import BudgetDenied
from app.schemas.common import PrivacyLevel
from app.tasks.models import TaskStatus

from .brief_models import BriefCommuteFetcher, BriefFact, BriefWeather, BriefWeatherFetcher
from .report_ports import ReportCalendarQuery, ReportContactQuery, ReportGoalQuery, ReportTaskQuery
from .report_rules import accepted_events, accepted_goals, bounded_text

MAX_TASK_FACTS = 5

MAX_GOAL_FACTS = 5

MAX_CONTACT_DATE_FACTS = 5

MAX_EVENT_FACTS = 8

MAX_TEXT_CHARS = 1_200


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _fact(*, kind: str, text: str, source: str) -> BriefFact:
    return BriefFact(kind=kind, text=bounded_text(text, 240), source=source)


def _commute_source(event_id: UUID | None, title: str) -> str:
    if event_id is not None:
        return f"calendar:{event_id}"
    source = f"commute:{title}"
    if len(source) <= 120:
        return source
    return "commute:title-sha256:" + sha256(title.encode("utf-8")).hexdigest()


class DailyBriefCollector:
    def __init__(
        self,
        task_store: ReportTaskQuery,
        cognitive_store: ReportGoalQuery,
        *,
        calendar_store: ReportCalendarQuery | None = None,
        timezone_name: str = "Asia/Shanghai",
        clock: Callable[[], datetime] | None = None,
        contact_store: ReportContactQuery | None = None,
        weather_fetcher: BriefWeatherFetcher | None = None,
        commute_fetcher: BriefCommuteFetcher | None = None,
    ) -> None:
        self._tasks = task_store
        self._goals = cognitive_store
        self._calendar = calendar_store
        self._tz = ZoneInfo(timezone_name)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._contacts = contact_store
        self._weather = weather_fetcher
        self._commute = commute_fetcher

    async def collect_facts(self, user_id: UUID, *, brief_date: date) -> list[BriefFact]:
        day_start = datetime.combine(brief_date, dt_time.min, tzinfo=self._tz)
        day_end = day_start + timedelta(days=1)
        facts: list[BriefFact] = []

        weather: BriefWeather | None = None
        if self._weather is not None:
            try:
                weather = await self._weather()
            except BudgetDenied:
                raise
            except Exception:
                weather = None
        if weather is not None:
            summary = f"{weather.city} {weather.condition} {weather.temperature_c}°C"
            if weather.low_c and weather.high_c:
                summary += f"（今日 {weather.low_c}~{weather.high_c}°C）"
            facts.append(_fact(kind="weather", text=summary, source="amap:weather"))

        if self._contacts is not None:
            matched_contacts = await self._contacts.contacts_with_date(
                user_id, month=brief_date.month, day=brief_date.day
            )
            matched_contacts = [
                contact.model_copy(deep=True)
                for contact in matched_contacts
                if contact.user_id == user_id
                and any(
                    item.month == brief_date.month and item.day == brief_date.day
                    for item in contact.important_dates
                )
            ]
            for contact in matched_contacts[:MAX_CONTACT_DATE_FACTS]:
                labels = "、".join(
                    item.label
                    for item in contact.important_dates
                    if item.month == brief_date.month and item.day == brief_date.day
                )
                facts.append(
                    _fact(
                        kind="contact_date",
                        text=f"今天是{contact.display_name}的{labels}",
                        source=f"contact:{contact.id}",
                    )
                )

        # 当日日程（本地 + CalDAV/Google 镜像同表；取消的不进简报）
        if self._calendar is not None:
            try:
                # SQLite 把 aware 值按本地墙钟绑定，而镜像行存 UTC 墙钟：
                # 查询边界统一归一到 UTC，保证两种存储语义一致（PG 无差别）。
                events = await self._calendar.list_events(
                    user_id,
                    starts_from=day_start.astimezone(UTC),
                    starts_to=day_end.astimezone(UTC),
                    include_cancelled=False,
                    limit=100,
                )
            except BudgetDenied:
                raise
            except Exception:
                events = []
            events = accepted_events(events, user_id, day_start, day_end)
            timed = sorted(
                (event for event in events if not event.all_day),
                key=lambda item: item.starts_at,
            )
            for event in timed[:MAX_EVENT_FACTS]:
                mirror_tag = ""
                if event.source and event.source != "api":
                    mirror_tag = f"[{event.source}] "
                fact_text = (
                    f"{mirror_tag}{event.starts_at.astimezone(self._tz).strftime('%H:%M')}"
                    f"-{event.ends_at.astimezone(self._tz).strftime('%H:%M')} {event.title}"
                )
                if event.location:
                    fact_text += f" @{event.location}"
                facts.append(
                    _fact(
                        kind="event",
                        text=fact_text,
                        source=f"calendar:{event.id}",
                    )
                )
            all_day_events = [event for event in events if event.all_day]
            if all_day_events:
                names = "、".join(event.title for event in all_day_events[:MAX_EVENT_FACTS])
                extra = len(all_day_events) - MAX_EVENT_FACTS
                suffix = f" 等{len(all_day_events)}条" if extra > 0 else ""
                facts.append(
                    _fact(
                        kind="event",
                        text=f"全天：{names}{suffix}",
                        source=f"calendar:{all_day_events[0].id}",
                    )
                )

        # 通勤建议（当日首个带地点日程；只读计算，不建提醒）
        if self._commute is not None:
            try:
                suggestion = await self._commute(user_id)
            except BudgetDenied:
                raise
            except Exception:
                suggestion = None
            starts_at = _aware(suggestion.starts_at) if suggestion is not None else None
            if (
                suggestion is not None
                and (suggestion.user_id is None or suggestion.user_id == user_id)
                and starts_at is not None
                and day_start <= starts_at < day_end
            ):
                duration_note = (
                    f"，{suggestion.mode}约 {suggestion.duration_min} 分钟"
                    if suggestion.duration_min
                    else ""
                )
                facts.append(
                    _fact(
                        kind="commute",
                        text=(
                            f"建议 {suggestion.leave_by.astimezone(self._tz).strftime('%H:%M')}"
                            f" 出发前往 {suggestion.destination}"
                            f"（{suggestion.event_title} "
                            f"{suggestion.starts_at.astimezone(self._tz).strftime('%H:%M')} 开始"
                            f"{duration_note}）"
                        ),
                        source=_commute_source(suggestion.event_id, suggestion.event_title),
                    )
                )

        due_tasks = []
        for task in await self._tasks.list_tasks(
            user_id, status=TaskStatus.ACTIVE, limit=200, max_privacy_level=PrivacyLevel.L1
        ):
            if (
                task.user_id != user_id
                or task.status != TaskStatus.ACTIVE
                or task.privacy_level not in {PrivacyLevel.L0, PrivacyLevel.L1}
            ):
                continue
            next_fire = _aware(task.next_fire_at)
            if next_fire is None or task.trigger.type != "time":
                continue
            if day_start <= next_fire < day_end:
                due_tasks.append((next_fire, task))
        due_tasks.sort(key=lambda item: item[0])
        for next_fire, task in due_tasks[:MAX_TASK_FACTS]:
            facts.append(
                _fact(
                    kind="task",
                    text=f"{next_fire.astimezone(self._tz).strftime('%H:%M')} {task.title}",
                    source=f"task:{task.id}",
                )
            )

        overdue_goals = []
        for goal in accepted_goals(
            await self._goals.active_goals(
                user_id, now=self._clock(), max_privacy_level=PrivacyLevel.L1
            ),
            now=self._clock(),
            active=True,
        ):
            due = _aware(goal.due_at)
            if due is None or due >= day_end:
                continue
            overdue_goals.append((due, goal))
        overdue_goals.sort(key=lambda item: item[0])
        for due, goal in overdue_goals[:MAX_GOAL_FACTS]:
            prefix = "已到期" if due < day_start else due.astimezone(self._tz).strftime("%H:%M")
            facts.append(
                _fact(
                    kind="goal",
                    text=f"[{prefix}] {goal.title}",
                    source=f"goal:{goal.id}",
                )
            )
        return facts


def compose_brief_text(brief_date: date, facts: list[BriefFact]) -> str:
    """确定性拼装：无重要内容时保持一行短句。"""
    weekdays = "一二三四五六日"
    header = f"{brief_date.month}月{brief_date.day}日 周{weekdays[brief_date.weekday()]}"

    weather = [fact.text for fact in facts if fact.kind == "weather"]
    events = [fact.text for fact in facts if fact.kind == "event"]
    commute = [fact.text for fact in facts if fact.kind == "commute"]
    tasks = [fact.text for fact in facts if fact.kind == "task"]
    goals = [fact.text for fact in facts if fact.kind == "goal"]
    contact_dates = [fact.text for fact in facts if fact.kind == "contact_date"]

    lines: list[str] = []
    if weather:
        lines.append(f"{header} · {weather[0]}")
    else:
        lines.append(header)
    if contact_dates:
        lines.append(f"今日重要日期（{len(contact_dates)}）：")
        lines.extend(f"· {item}" for item in contact_dates)
    if events:
        lines.append(f"今日日程（{len(events)}）：")
        lines.extend(f"· {item}" for item in events)
    if commute:
        lines.append(f"出行建议：{commute[0]}")
    if tasks:
        lines.append(f"今日待办（{len(tasks)}）：")
        lines.extend(f"· {item}" for item in tasks)
    if goals:
        lines.append(f"到期承诺（{len(goals)}）：")
        lines.extend(f"· {item}" for item in goals)
    if not tasks and not goals and not contact_dates and not events and not commute:
        lines.append("今天没有到期的任务或承诺。")
    text = "\n".join(lines)
    if len(text) > MAX_TEXT_CHARS:
        text = text[: MAX_TEXT_CHARS - 1] + "…"
    return text
