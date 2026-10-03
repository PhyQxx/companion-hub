"""Owned report collection from explicit DTO query ports, without SQL or SDKs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from uuid import UUID
from zoneinfo import ZoneInfo

from app.harness.budget import BudgetDenied
from app.schemas.common import PrivacyLevel
from app.tasks.models import TaskStatus

from .report_ports import ReportCalendarQuery, ReportGoalQuery, ReportTaskQuery
from .report_rules import accepted_events, accepted_goals, bounded_text
from .review_models import _SECTION_TITLES, ReviewItem, ReviewSection

MAX_ITEMS_PER_SECTION = 5

MAX_TEXT_CHARS = 1_600


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _item(*, section: ReviewSection, text: str, source: str) -> ReviewItem:
    return ReviewItem(section=section, text=bounded_text(text, 280), source=source)


class DailyReviewCollector:
    def __init__(
        self,
        task_store: ReportTaskQuery,
        cognitive_store: ReportGoalQuery,
        *,
        calendar_store: ReportCalendarQuery | None = None,
        timezone_name: str = "Asia/Shanghai",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._tasks = task_store
        self._goals = cognitive_store
        self._calendar = calendar_store
        self._tz = ZoneInfo(timezone_name)
        self._clock = clock or (lambda: datetime.now(UTC))

    async def collect_items(self, user_id: UUID, *, review_date: date) -> list[ReviewItem]:
        day_start = datetime.combine(review_date, dt_time.min, tzinfo=self._tz)
        day_end = day_start + timedelta(days=1)
        tomorrow_end = day_end + timedelta(days=1)
        items: list[ReviewItem] = []

        for task in await self._tasks.list_tasks(
            user_id, status=TaskStatus.DONE, limit=200, max_privacy_level=PrivacyLevel.L1
        ):
            if (
                task.user_id != user_id
                or task.status != TaskStatus.DONE
                or task.privacy_level not in {PrivacyLevel.L0, PrivacyLevel.L1}
            ):
                continue
            completed = _aware(task.completed_at)
            if completed is None or not day_start <= completed < day_end:
                continue
            items.append(
                _item(
                    section="completed",
                    text=task.title,
                    source=f"task:{task.id}",
                )
            )
        for goal in accepted_goals(
            await self._goals.goals_completed_between(
                user_id, start=day_start, end=day_end, max_privacy_level=PrivacyLevel.L1
            ),
            now=self._clock(),
            active=False,
            completed=True,
        ):
            items.append(_item(section="completed", text=goal.title, source=f"goal:{goal.id}"))

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
            if task.trigger.type != "time" or next_fire is None or next_fire >= day_end:
                continue
            overdue = (
                "已逾期"
                if next_fire < day_start
                else next_fire.astimezone(self._tz).strftime("%H:%M")
            )
            items.append(
                _item(
                    section="unfinished",
                    text=f"[{overdue}] {task.title}",
                    source=f"task:{task.id}",
                )
            )
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
            items.append(_item(section="unfinished", text=goal.title, source=f"goal:{goal.id}"))

        for goal in accepted_goals(
            await self._goals.goals_created_between(
                user_id, start=day_start, end=day_end, max_privacy_level=PrivacyLevel.L1
            ),
            now=self._clock(),
            active=False,
        ):
            items.append(_item(section="new_commitment", text=goal.title, source=f"goal:{goal.id}"))

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
            if task.trigger.type != "time" or next_fire is None:
                continue
            if not day_end <= next_fire < tomorrow_end:
                continue
            items.append(
                _item(
                    section="tomorrow",
                    text=f"{next_fire.astimezone(self._tz).strftime('%H:%M')} {task.title}",
                    source=f"task:{task.id}",
                )
            )
        for goal in accepted_goals(
            await self._goals.active_goals(
                user_id, now=self._clock(), max_privacy_level=PrivacyLevel.L1
            ),
            now=self._clock(),
            active=True,
        ):
            due = _aware(goal.due_at)
            if due is None or not day_end <= due < tomorrow_end:
                continue
            items.append(
                _item(
                    section="tomorrow",
                    text=goal.title,
                    source=f"goal:{goal.id}",
                )
            )

        # 明日日程（本地 + 外部镜像；全天日程只列标题）
        if self._calendar is not None:
            try:
                # 与 brief 同因：查询边界归一到 UTC，兼容 SQLite 墙钟存储
                events = await self._calendar.list_events(
                    user_id,
                    starts_from=day_end.astimezone(UTC),
                    starts_to=tomorrow_end.astimezone(UTC),
                    include_cancelled=False,
                    limit=50,
                )
            except BudgetDenied:
                raise
            except Exception:
                events = []
            events = accepted_events(events, user_id, day_end, tomorrow_end)
            for event in sorted(
                (item for item in events if not item.all_day),
                key=lambda item: item.starts_at,
            ):
                mirror_tag = f"[{event.source}] " if event.source and event.source != "api" else ""
                items.append(
                    _item(
                        section="tomorrow",
                        text=(
                            f"{mirror_tag}{event.starts_at.astimezone(self._tz).strftime('%H:%M')}"
                            f" {event.title}"
                        ),
                        source=f"calendar:{event.id}",
                    )
                )
            for event in (item for item in events if item.all_day):
                items.append(
                    _item(
                        section="tomorrow",
                        text=f"全天：{event.title}",
                        source=f"calendar:{event.id}",
                    )
                )

        return _cap_sections(items)


def _cap_sections(items: list[ReviewItem]) -> list[ReviewItem]:
    capped: list[ReviewItem] = []
    counts: dict[str, int] = {}
    for item in items:
        count = counts.get(item.section, 0)
        if count >= MAX_ITEMS_PER_SECTION:
            continue
        counts[item.section] = count + 1
        capped.append(item)
    return capped


def compose_review_text(review_date: date, items: list[ReviewItem]) -> str:
    """确定性拼装：removed 的条目不再出现，note 作为更正附注展示。"""
    weekdays = "一二三四五六日"
    header = (
        f"{review_date.month}月{review_date.day}日（周{weekdays[review_date.weekday()]}）晚间回顾"
    )
    lines: list[str] = [header]
    visible = [item for item in items if item.action != "removed"]
    for section in ("completed", "unfinished", "new_commitment", "tomorrow"):
        section_items = [item for item in visible if item.section == section]
        if not section_items:
            continue
        lines.append(f"{_SECTION_TITLES[section]}（{len(section_items)}）：")
        for item in section_items:
            line = f"· {item.text}"
            if item.note:
                line += f"（用户更正：{item.note}）"
            lines.append(line)
    if len(lines) == 1:
        lines.append("今天没有需要回顾的事项。")
    text = "\n".join(lines)
    if len(text) > MAX_TEXT_CHARS:
        text = text[: MAX_TEXT_CHARS - 1] + "…"
    return text
