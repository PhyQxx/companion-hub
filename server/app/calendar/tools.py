"""CAL-01 日程工具：只准备预览，鉴权 UI 确认后才能落库。"""

from __future__ import annotations

from datetime import datetime
from time import perf_counter
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.confirmation import DatabasePendingMutationStore, PendingMutationStore
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
        "准备日历日程预览（默认会前 10 分钟提醒）。用户必须在聊天卡片核对时间、地点、"
        "参与人和提醒后点击确认创建；confirmed=true 也不会写入。返回冲突时告知用户并"
        "建议改期。starts_at/ends_at 使用用户所在时区本地时间，ISO 格式。"
    )
    arguments_model: type[BaseModel] = CalendarCreateArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    def __init__(
        self,
        service: CalendarService,
        *,
        timezone_name: str = "Asia/Shanghai",
        drafts: PendingMutationStore | DatabasePendingMutationStore | None = None,
    ) -> None:
        self._service = service
        self._timezone = timezone_name
        self._drafts = drafts or PendingMutationStore()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=CalendarCreateArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = self._cast(arguments)
        # ToolContext 经 pydantic use_enum_values 校验后是普通字符串，必须用 == 比较
        if context.privacy_level == PrivacyLevel.L2:
            return self._failure("private_session_unsupported", started)
        if context.turn_id is None or context.user_id is None:
            return self._failure("idempotency_key_missing", started)
        if args.confirmed:
            return self._failure("user_confirmation_required", started)
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
        preview_data = {
            "title": preview.title,
            "starts_at": preview.starts_at.isoformat(),
            "ends_at": preview.ends_at.isoformat(),
            "location": preview.location,
            "participants": [item.name for item in preview.participants],
            "calendar_id": preview.calendar_id,
            "reminder_lead_minutes": preview.reminder_lead_minutes,
        }
        try:
            draft = await self._drafts.prepare(
                user_id=context.user_id,
                turn_id=context.turn_id,
                kind="calendar_create",
                content=preview_data,
                preview=preview_data,
            )
        except OverflowError:
            return self._failure("confirmation_preview_capacity", started)
        return ToolResult(
            ok=True,
            tool_name=self.name,
            data={
                "created": False,
                "confirmation_required": draft.status == "pending",
                "draft_id": str(draft.id),
                "status": draft.status,
                "preview": draft.preview,
                "expires_at": draft.expires_at.isoformat(),
            },
            latency_ms=(perf_counter() - started) * 1_000,
        )

    def _cast(self, arguments: BaseModel) -> CalendarCreateArgs:
        return cast(CalendarCreateArgs, arguments)

    async def list_drafts(self, user_id: UUID) -> list[dict[str, object]]:
        return [item.view() for item in await self._drafts.list(user_id)]

    async def cancel(self, user_id: UUID, draft_id: UUID) -> dict[str, object]:
        return (await self._drafts.cancel(user_id, draft_id)).view()

    async def confirm(
        self, user_id: UUID, draft_id: UUID, digest: str
    ) -> dict[str, object]:
        draft = await self._drafts.claim(user_id, draft_id, digest)
        if draft.status == "completed":
            return draft.view()
        content = draft.content
        try:
            view = await self._service.create_event(
                user_id,
                title=str(content["title"]),
                starts_at=datetime.fromisoformat(str(content["starts_at"])),
                ends_at=datetime.fromisoformat(str(content["ends_at"])),
                calendar_id=str(content["calendar_id"]),
                location=str(content["location"]) if content["location"] is not None else None,
                participants=[
                    CalendarParticipant(name=str(name))
                    for name in cast(list[object], content["participants"])
                ],
                reminder_lead_minutes=int(cast(int, content["reminder_lead_minutes"])),
            )
        except BaseException:
            await self._drafts.mark_unknown(draft)
            raise
        return (await self._drafts.complete(draft, _event_payload(view))).view()

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


class CalendarSyncArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["caldav", "google", "all"] = "all"


class CalendarSyncTool:
    """CAL-01 手动触发外部日历镜像同步（聊天入口，仅 L1）。"""

    name = "calendar_sync"
    description = (
        "同步外部日历（CalDAV / Google）到本地镜像：用户要求同步日历、刷新"
        "日程时调用。返回各提供方拉取与镜像数量；未配置或未授权的提供方会在"
        " errors 里说明。仅 L1 可用。"
    )
    arguments_model: type[BaseModel] = CalendarSyncArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    def __init__(
        self,
        caldav_sync: Any | None = None,
        google_sync: Any | None = None,
    ) -> None:
        self._caldav = caldav_sync
        self._google = google_sync

    @property
    def available(self) -> bool:
        return self._caldav is not None or self._google is not None

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=CalendarSyncArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(CalendarSyncArgs, arguments)
        if context.privacy_level == PrivacyLevel.L2:
            return self._failure("private_session_unsupported", started)
        providers: list[str] = (
            ["caldav", "google"] if args.provider == "all" else [args.provider]
        )
        results: dict[str, dict[str, object]] = {}
        for provider in providers:
            service = self._caldav if provider == "caldav" else self._google
            if service is None:
                results[provider] = {"errors": ["not_configured"]}
                continue
            try:
                stats = await service.sync_once()
            except Exception:
                results[provider] = {"errors": ["sync_failed"]}
                continue
            results[provider] = {
                "calendars": stats.calendars,
                "pulled": stats.pulled,
                "mirrors_created": stats.mirrors_created,
                "mirrors_updated": stats.mirrors_updated,
                "mirrors_cancelled": stats.mirrors_cancelled,
                "errors": stats.errors,
            }
        ok = any(not result["errors"] for result in results.values())
        return ToolResult(
            ok=ok,
            tool_name=self.name,
            data={"providers": results},
            reason_code=None if ok else "calendar_sync_failed",
            latency_ms=(perf_counter() - started) * 1_000,
        )

    def _failure(self, reason: str, started: float) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=self.name,
            reason_code=reason,
            latency_ms=(perf_counter() - started) * 1_000,
        )
