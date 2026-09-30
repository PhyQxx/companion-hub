"""TASK-01 聊天端提醒工具：创建、查看与关闭。

安全边界：挂载门禁在 ChatService `_device_tool_ready`（仅 L1 开放；L0 公开
模式不读写个人数据，L2 私密会话内容不入库），这里再做执行级兜底拒绝。
创建同一回合重复调用按 turn 幂等返回已创建的任务，不重复写入。

关闭按标题一步匹配（精确→包含）：默认 max_tool_rounds=1，模型没有
「先查列表再按 id 关闭」的两回合预算，必须在单次调用内解析目标。
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from time import perf_counter
from typing import Annotated, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

from app.llm import ToolDefinition
from app.schemas.common import PrivacyLevel
from app.tasks.models import RepeatKind, TaskKind, TaskStatus, TaskTrigger, TaskView
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
                data={
                    "created": False,
                    "duplicate": True,
                    **_task_payload(existing, self._timezone),
                },
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
                    PrivacyLevel.L0 if context.privacy_level == PrivacyLevel.L0 else PrivacyLevel.L1
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
            data={"created": True, **_task_payload(view, self._timezone)},
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


class ReminderListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["active", "done", "cancelled", "all"] = "active"


class ReminderListTool:
    name = "reminder_list"
    description = (
        "查看用户已创建的提醒（用户问「我有哪些提醒/还有什么要提醒我/明天几点有提醒」"
        "时使用）。返回每条的 id、标题、重复方式、下次触发时间与状态；"
        "用户要关闭或取消某条提醒时改用 reminder_cancel，不要在本工具结果上追问 id。"
    )
    arguments_model: type[BaseModel] = ReminderListArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    _MAX_RETURNED = 30

    def __init__(
        self,
        store: TaskStore,
        *,
        timezone_name: str = "Asia/Shanghai",
    ) -> None:
        self._store = store
        self._timezone = timezone_name

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=ReminderListArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(ReminderListArgs, arguments)
        if context.privacy_level == PrivacyLevel.L2:
            return self._failure("private_session_unsupported", started)
        if context.user_id is None:
            return self._failure("user_context_missing", started)
        status = None if args.status == "all" else TaskStatus(args.status)
        tasks = await self._store.list_tasks(context.user_id, status=status)
        reminders = [task for task in tasks if task.kind == TaskKind.REMINDER]
        trimmed = reminders[: self._MAX_RETURNED]
        return ToolResult(
            ok=True,
            tool_name=self.name,
            data={
                "count": len(trimmed),
                "total": len(reminders),
                "tasks": [_task_summary(task, self._timezone) for task in trimmed],
            },
            latency_ms=(perf_counter() - started) * 1_000,
        )

    def _failure(self, reason: str, started: float) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=self.name,
            reason_code=reason,
            latency_ms=(perf_counter() - started) * 1_000,
        )


class ReminderCancelArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # reminder_list 结果里的任务 id；用户复述过 id 或上一轮候选列表里有 id 时使用
    task_id: Annotated[str, Field(min_length=8, max_length=64)] | None = None
    # 提醒标题（用户只说了名称时使用），在生效提醒中做精确→包含匹配
    title: Annotated[str, Field(min_length=1, max_length=320)] | None = None
    # done=事项已办完；cancel=不想要这条提醒了。两者都会停止后续触发
    mark: Literal["cancel", "done"] = "cancel"


class ReminderCancelTool:
    name = "reminder_cancel"
    description = (
        "关闭用户的某条提醒（用户说「这个提醒不要了/关掉/已经去过了/取消那条」时使用）。"
        "用户直接说了提醒名称时传 title 即可，系统会在生效提醒里做精确→包含匹配，"
        "无需先调用 reminder_list；已知任务 id 时传 task_id 精确指定。"
        "唯一命中即关闭；多个命中会返回候选列表，需向用户确认关哪条；"
        "没有命中则如实告知未找到。mark=done 表示事项已完成，默认 cancel。"
    )
    arguments_model: type[BaseModel] = ReminderCancelArgs
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

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=ReminderCancelArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(ReminderCancelArgs, arguments)
        if context.privacy_level == PrivacyLevel.L2:
            return self._failure("private_session_unsupported", started, {"changed": False})
        if context.user_id is None:
            return self._failure("user_context_missing", started, {"changed": False})
        if (args.task_id is None) == (args.title is None):
            return self._failure(
                "invalid_arguments",
                started,
                {"changed": False, "reason_detail": "task_id 与 title 必须二选一"},
            )
        matches, reason = await self._resolve(context.user_id, args)
        if reason is not None:
            return self._failure(reason, started, {"changed": False})
        if not matches:
            return self._failure(
                "task_not_found",
                started,
                {
                    "changed": False,
                    "hint": "没有找到匹配的提醒，可调用 reminder_list 查看现有提醒",
                },
            )
        if len(matches) > 1:
            return self._failure(
                "ambiguous_match",
                started,
                {
                    "changed": False,
                    "candidates": [_task_summary(task, self._timezone) for task in matches],
                },
            )
        target = matches[0]
        if target.status != TaskStatus.ACTIVE:
            return ToolResult(
                ok=True,
                tool_name=self.name,
                data={
                    "changed": False,
                    "status": str(target.status),
                    "task": _task_summary(target, self._timezone),
                },
                latency_ms=(perf_counter() - started) * 1_000,
            )
        try:
            view = (
                await self._store.complete_task(context.user_id, target.id, now=self._clock())
                if args.mark == "done"
                else await self._store.cancel_task(context.user_id, target.id, now=self._clock())
            )
        except ValueError:
            # 并发下状态已变（如刚好触发转 firing/done），以最新状态幂等返回
            view = await self._store.get_task(context.user_id, target.id)
            return ToolResult(
                ok=True,
                tool_name=self.name,
                data={
                    "changed": False,
                    "status": str(view.status),
                    "task": _task_summary(view, self._timezone),
                },
                latency_ms=(perf_counter() - started) * 1_000,
            )
        return ToolResult(
            ok=True,
            tool_name=self.name,
            data={
                "changed": True,
                "mark": "done" if args.mark == "done" else "cancelled",
                "task": _task_summary(view, self._timezone),
            },
            latency_ms=(perf_counter() - started) * 1_000,
        )

    async def _resolve(
        self, user_id: UUID, args: ReminderCancelArgs
    ) -> tuple[list[TaskView], str | None]:
        """解析关闭目标：按 id 直取；按标题在生效提醒中匹配。"""
        if args.task_id is not None:
            try:
                task_id = UUID(args.task_id)
            except ValueError:
                return [], "invalid_arguments"
            try:
                task = await self._store.get_task(user_id, task_id)
            except LookupError:
                return [], "task_not_found"
            if task.source == "pnkx":
                # pnkx 是外部真源，本地关闭会造成双端状态漂移，拒绝改走镜像协议
                return [], "external_mirror_unsupported"
            return [task], None
        assert args.title is not None
        active = [
            task
            for task in await self._store.list_tasks(user_id, status=TaskStatus.ACTIVE)
            if task.kind == TaskKind.REMINDER and task.source != "pnkx"
        ]
        wanted = args.title.strip().casefold()
        exact = [task for task in active if task.title.strip().casefold() == wanted]
        if exact:
            return exact, None
        partial = [
            task
            for task in active
            if wanted in task.title.casefold() or task.title.casefold() in wanted
        ]
        return partial, None

    def _failure(
        self, reason: str, started: float, data: dict[str, object] | None = None
    ) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=self.name,
            reason_code=reason,
            data=data or {},
            latency_ms=(perf_counter() - started) * 1_000,
        )


def _task_summary(view: TaskView, timezone_name: str | None = None) -> dict[str, object]:
    next_fire = view.next_fire_at
    if next_fire is not None and timezone_name is not None:
        with suppress(ZoneInfoNotFoundError, ValueError):
            next_fire = next_fire.astimezone(ZoneInfo(timezone_name))
    return {
        "id": str(view.id),
        "title": view.title,
        "repeat": str(view.trigger.repeat_kind),
        "event": view.trigger.event_type,
        "status": str(view.status),
        "source": view.source,
        "next_fire_at": next_fire.isoformat() if next_fire is not None else None,
    }


def _task_payload(view: TaskView, timezone_name: str | None = None) -> dict[str, object]:
    return {"task": _task_summary(view, timezone_name)}
