"""TASK-01 聊天端建提醒工具：把模型规范化后的参数写入任务存储。

安全边界：挂载门禁在 ChatService `_device_tool_ready`（仅 L1 开放；L0 公开
模式不写入个人数据，L2 私密会话内容不入库），这里再做执行级兜底拒绝。
同一回合重复调用按 turn 幂等返回已创建的任务，不重复写入。
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from time import perf_counter
from typing import Annotated, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

from app.llm import ToolDefinition
from app.schemas.common import PrivacyLevel
from app.tasks.models import RepeatKind, TaskKind, TaskTrigger, TaskView
from app.tasks.store import TaskStore
from app.tools.contracts import ToolContext, ToolResult

_REPEAT_MAP: dict[str, RepeatKind] = {
    "once": RepeatKind.ONCE,
    "daily": RepeatKind.DAILY,
    "weekdays": RepeatKind.WEEKDAYS,
    "weekly": RepeatKind.WEEKLY,
    "interval": RepeatKind.INTERVAL,
}


def _default_clock() -> datetime:
    return datetime.now(UTC)


def localize(value: datetime, timezone_name: str) -> datetime:
    """模型按用户本地时间传参；无偏移的时间按 Hub 默认时区解释。"""
    if value.tzinfo is not None:
        return value
    try:
        return value.replace(tzinfo=ZoneInfo(timezone_name))
    except (ZoneInfoNotFoundError, ValueError):
        return value.replace(tzinfo=ZoneInfo("UTC"))


class ReminderCreateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: Annotated[str, Field(min_length=1, max_length=320)]
    # 首次触发时间（周期任务同时作为复现锚点），用户时区本地时间。
    at: datetime | None = None
    repeat: Literal["once", "daily", "weekdays", "weekly", "interval"] = "once"
    interval_minutes: Annotated[int, Field(ge=1, le=525_600)] | None = None
    # weekly 专用：0=周一 … 6=周日
    weekdays: list[Annotated[int, Field(ge=0, le=6)]] | None = None
    # 事件触发：用户说「到家/离家时提醒我」时使用，替代时间参数
    event: Literal["user_arrived_home", "user_left_home"] | None = None
    notes: Annotated[str, Field(max_length=2000)] | None = None


class ReminderCreateTool:
    name = "reminder_create"
    description = (
        "为用户创建本地提醒或周期任务（用户说「提醒我/到点叫我/每天几点提醒/到家时提醒我」"
        "等明确请求时使用）。at 使用用户所在时区的本地时间，ISO 格式 YYYY-MM-DDTHH:MM；"
        "repeat 支持 once/daily/weekdays/weekly/interval（interval 需给 interval_minutes，"
        "weekly 需给 weekdays，0=周一）。信息不完整（缺具体时间或提醒事项）时先追问，"
        "不要猜；创建后用一句话向用户复述提醒内容与触发时间。"
    )
    arguments_model: type[BaseModel] = ReminderCreateArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    def __init__(
        self,
        store: TaskStore,
        *,
        timezone_name: str = "Asia/Shanghai",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._timezone = timezone_name
        self._clock = clock or _default_clock
        # 同回合幂等：工具循环里模型重放同一调用时返回已建任务，不重复写入。
        self._created_by_turn: OrderedDict[UUID, TaskView] = OrderedDict()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=ReminderCreateArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(ReminderCreateArgs, arguments)
        # ToolContext 经 pydantic use_enum_values 校验后是普通字符串，必须用 == 比较
        if context.privacy_level == PrivacyLevel.L2:
            return self._failure("private_session_unsupported", started)
        if context.turn_id is None or context.user_id is None:
            return self._failure("idempotency_key_missing", started)
        existing = self._created_by_turn.get(context.turn_id)
        if existing is not None:
            return ToolResult(
                ok=True,
                tool_name=self.name,
                data={"created": False, "duplicate": True, **_task_payload(existing)},
                latency_ms=(perf_counter() - started) * 1_000,
            )
        trigger = self._trigger(args)
        try:
            view = await self._store.create(
                user_id=context.user_id,
                kind=TaskKind.REMINDER,
                title=args.title,
                notes=args.notes,
                trigger=trigger,
                privacy_level=(
                    PrivacyLevel.L0
                    if context.privacy_level == PrivacyLevel.L0
                    else PrivacyLevel.L1
                ),
                source="chat",
                now=self._clock(),
            )
        except ValueError as error:
            return ToolResult(
                ok=False,
                tool_name=self.name,
                reason_code="invalid_trigger",
                data={"created": False, "reason_detail": str(error)},
                latency_ms=(perf_counter() - started) * 1_000,
            )
        self._remember(context.turn_id, view)
        return ToolResult(
            ok=True,
            tool_name=self.name,
            data={"created": True, **_task_payload(view)},
            latency_ms=(perf_counter() - started) * 1_000,
        )

    def _trigger(self, args: ReminderCreateArgs) -> TaskTrigger:
        if args.event is not None:
            return TaskTrigger(type="event", event_type=args.event)
        at = localize(args.at, self._timezone) if args.at is not None else None
        return TaskTrigger(
            type="time",
            at=at,
            repeat_kind=_REPEAT_MAP[args.repeat],
            interval_minutes=args.interval_minutes,
            weekdays=args.weekdays,
        )

    def _remember(self, turn_id: UUID, view: TaskView) -> None:
        self._created_by_turn[turn_id] = view
        while len(self._created_by_turn) > 128:
            self._created_by_turn.popitem(last=False)

    def _failure(self, reason: str, started: float) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=self.name,
            reason_code=reason,
            latency_ms=(perf_counter() - started) * 1_000,
        )


def _task_payload(view: TaskView) -> dict[str, object]:
    return {
        "task": {
            "id": str(view.id),
            "title": view.title,
            "repeat": str(view.trigger.repeat_kind),
            "event": view.trigger.event_type,
            "next_fire_at": view.next_fire_at.isoformat()
            if view.next_fire_at is not None
            else None,
        },
    }
