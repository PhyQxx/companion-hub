"""CAL-01 聊天端建日程工具：先预览（时间规范化 + 冲突检查），确认后落库。

两段式契约对应验收「写入前展示」：第一次调用不携带 confirmed，只返回
预览与冲突；模型把预览复述给用户，用户明确同意后再带 confirmed=true
创建。时间冲突是服务端硬闸门——即使 confirmed=true 也拒绝落库。
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from time import perf_counter
from typing import Annotated, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.llm import ToolDefinition
from app.schemas.common import PrivacyLevel
from app.tasks.tools import localize
from app.tools.contracts import ToolContext, ToolResult

from .models import CalendarEventView, CalendarParticipant
from .service import CalendarService


class CalendarCreateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: Annotated[str, Field(min_length=1, max_length=320)]
    starts_at: datetime
    ends_at: datetime
    location: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    # 参与人姓名列表（v1 本地日历不发送邀请，仅作展示与冲突提示）
    participants: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(
        default_factory=list, max_length=20
    )
    reminder_lead_minutes: Annotated[int, Field(ge=0, le=1440)] = 10
    confirmed: bool = False


class CalendarCreateTool:
    name = "calendar_create"
    description = (
        "为用户创建日历日程（默认会前 10 分钟提醒）。首次调用不要传 confirmed 或传 false："
        "只做时间规范化与冲突检查，把返回的预览（时间、地点、参与人、提前提醒量）复述给"
        "用户；用户明确同意后再带 confirmed=true 创建。返回冲突时必须告知用户冲突日程并"
        "建议改期，不要直接创建。starts_at/ends_at 使用用户所在时区本地时间，ISO 格式。"
    )
    arguments_model: type[BaseModel] = CalendarCreateArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    def __init__(self, service: CalendarService, *, timezone_name: str = "Asia/Shanghai") -> None:
        self._service = service
        self._timezone = timezone_name
        self._created_by_turn: OrderedDict[UUID, CalendarEventView] = OrderedDict()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=CalendarCreateArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = self._cast(arguments)
        if context.privacy_level is PrivacyLevel.L2:
            return self._failure("private_session_unsupported", started)
        if context.turn_id is None or context.user_id is None:
            return self._failure("idempotency_key_missing", started)
        existing = self._created_by_turn.get(context.turn_id)
        if existing is not None:
            return ToolResult(
                ok=True,
                tool_name=self.name,
                data={"created": False, "duplicate": True, **_event_payload(existing)},
                latency_ms=(perf_counter() - started) * 1_000,
            )
        starts_at = localize(args.starts_at, self._timezone)
        ends_at = localize(args.ends_at, self._timezone)
        participants = [CalendarParticipant(name=name) for name in args.participants]
        try:
            preview = await self._service.preview(
                context.user_id,
                title=args.title,
                starts_at=starts_at,
                ends_at=ends_at,
                location=args.location,
                participants=participants,
                reminder_lead_minutes=args.reminder_lead_minutes,
            )
        except ValueError as error:
            return ToolResult(
                ok=False,
                tool_name=self.name,
                reason_code="invalid_time_window",
                data={"created": False, "reason_detail": str(error)},
                latency_ms=(perf_counter() - started) * 1_000,
            )
        if preview.conflicts:
            return ToolResult(
                ok=True,
                tool_name=self.name,
                data={
                    "created": False,
                    "time_conflict": True,
                    "conflicts": [_event_summary(item) for item in preview.conflicts],
                },
                latency_ms=(perf_counter() - started) * 1_000,
            )
        if not args.confirmed:
            return ToolResult(
                ok=True,
                tool_name=self.name,
                data={
                    "created": False,
                    "confirmation_required": True,
                    "preview": {
                        "title": preview.title,
                        "starts_at": preview.starts_at.isoformat(),
                        "ends_at": preview.ends_at.isoformat(),
                        "location": preview.location,
                        "participants": [item.name for item in preview.participants],
                        "calendar_id": preview.calendar_id,
                        "reminder_lead_minutes": preview.reminder_lead_minutes,
                    },
                },
                latency_ms=(perf_counter() - started) * 1_000,
            )
        try:
            view = await self._service.create_event(
                context.user_id,
                title=args.title,
                starts_at=starts_at,
                ends_at=ends_at,
                location=args.location,
                participants=participants,
                reminder_lead_minutes=args.reminder_lead_minutes,
            )
        except ValueError as error:
            return ToolResult(
                ok=False,
                tool_name=self.name,
                reason_code="invalid_time_window",
                data={"created": False, "reason_detail": str(error)},
                latency_ms=(perf_counter() - started) * 1_000,
            )
        self._remember(context.turn_id, view)
        return ToolResult(
            ok=True,
            tool_name=self.name,
            data={"created": True, **_event_payload(view)},
            latency_ms=(perf_counter() - started) * 1_000,
        )

    def _cast(self, arguments: BaseModel) -> CalendarCreateArgs:
        return cast(CalendarCreateArgs, arguments)

    def _remember(self, turn_id: UUID, view: CalendarEventView) -> None:
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


def _event_payload(view: CalendarEventView) -> dict[str, object]:
    summary = _event_summary(view)
    summary["reminder_task_id"] = str(view.reminder_task_id) if view.reminder_task_id else None
    return {"event": summary}


def _event_summary(view: CalendarEventView) -> dict[str, object]:
    return {
        "id": str(view.id),
        "title": view.title,
        "starts_at": view.starts_at.isoformat(),
        "ends_at": view.ends_at.isoformat(),
        "location": view.location,
        "participants": [item.name for item in view.participants],
    }
