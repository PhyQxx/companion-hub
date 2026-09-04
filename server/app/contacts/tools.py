"""CONTACT-01 聊天端联系人工具：显式保存与按名查询。

红线（对应验收「不自动推断敏感关系属性」）：contact_save 只在用户
明确要求记住/更新联系人时调用，relationship 与 preferences 必须来自
用户的原话陈述，禁止从对话内容自行推断或补全；contact_query 只读。
两者仅 L1 挂载（L0 公开模式不读个人数据，L2 私密会话内容不入库，
执行层对写操作兜底拒绝），同 turn 幂等。
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, date, datetime
from time import perf_counter
from typing import Annotated, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from app.llm import ToolDefinition
from app.schemas.common import PrivacyLevel
from app.tools.contracts import ToolContext, ToolResult

from .models import ContactImportantDate, ContactPreference, ContactView, days_until
from .store import ContactStore


class ContactSaveArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contact_id: UUID | None = None
    display_name: Annotated[str, Field(min_length=1, max_length=120)]
    aliases: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=12
    )
    # 用户明确说出的关系描述（如「我妈妈」）；不要自行推断或猜测
    relationship: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    timezone: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    important_dates: list[ContactImportantDate] = Field(default_factory=list, max_length=12)
    # 用户明确授权记录的偏好；不要从对话内容推断
    preferences: list[ContactPreference] = Field(default_factory=list, max_length=24)
    notes: Annotated[str, Field(max_length=2000)] | None = None


class ContactQueryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[str, Field(min_length=1, max_length=120)]


class ContactSaveTool:
    name = "contact_save"
    description = (
        "保存或更新一位联系人（名称、别名、时区、重要日期、偏好）。只在用户明确要求"
        "「记住这个人/更新联系人信息」时调用；关系与偏好必须来自用户的原话，"
        "不要从对话自行推断。不传 contact_id 时按名称匹配已有联系人进行更新，"
        "找不到才新建。timezone 用 IANA 名称（如 Asia/Tokyo）。"
    )
    arguments_model: type[BaseModel] = ContactSaveArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    def __init__(
        self, store: ContactStore, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._saved_by_turn: OrderedDict[UUID, ContactView] = OrderedDict()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=ContactSaveArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = self._cast(arguments)
        # ToolContext 经 pydantic use_enum_values 校验后是普通字符串，必须用 == 比较
        if context.privacy_level == PrivacyLevel.L2:
            return self._failure("private_session_unsupported", started)
        if context.turn_id is None or context.user_id is None:
            return self._failure("idempotency_key_missing", started)
        existing = self._saved_by_turn.get(context.turn_id)
        if existing is not None:
            return ToolResult(
                ok=True,
                tool_name=self.name,
                data={"saved": True, "duplicate": True, **_contact_payload(existing)},
                latency_ms=(perf_counter() - started) * 1_000,
            )
        try:
            view, created = await self._upsert(context.user_id, args)
        except ValueError as error:
            return ToolResult(
                ok=False,
                tool_name=self.name,
                reason_code="invalid_contact",
                data={"saved": False, "reason_detail": str(error)},
                latency_ms=(perf_counter() - started) * 1_000,
            )
        self._remember(context.turn_id, view)
        return ToolResult(
            ok=True,
            tool_name=self.name,
            data={"saved": True, "created": created, **_contact_payload(view)},
            latency_ms=(perf_counter() - started) * 1_000,
        )

    async def _upsert(self, user_id: UUID, args: ContactSaveArgs) -> tuple[ContactView, bool]:
        if args.contact_id is not None:
            view = await self._store.update_contact(
                user_id,
                args.contact_id,
                display_name=args.display_name,
                aliases=args.aliases,
                relationship=args.relationship,
                timezone=args.timezone,
                important_dates=args.important_dates,
                preferences=args.preferences,
                notes=args.notes,
                now=self._clock(),
            )
            return view, False
        matched = await self._store.find_by_name(user_id, args.display_name)
        if matched is not None:
            view = await self._store.update_contact(
                user_id,
                matched.id,
                display_name=args.display_name,
                aliases=args.aliases,
                relationship=args.relationship,
                timezone=args.timezone,
                important_dates=args.important_dates,
                preferences=args.preferences,
                notes=args.notes,
                now=self._clock(),
            )
            return view, False
        view = await self._store.create_contact(
            user_id=user_id,
            display_name=args.display_name,
            aliases=args.aliases,
            relationship=args.relationship,
            timezone=args.timezone,
            important_dates=args.important_dates,
            preferences=args.preferences,
            notes=args.notes,
            now=self._clock(),
        )
        return view, True

    def _cast(self, arguments: BaseModel) -> ContactSaveArgs:
        return cast(ContactSaveArgs, arguments)

    def _remember(self, turn_id: UUID, view: ContactView) -> None:
        self._saved_by_turn[turn_id] = view
        while len(self._saved_by_turn) > 128:
            self._saved_by_turn.popitem(last=False)

    def _failure(self, reason: str, started: float) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=self.name,
            reason_code=reason,
            latency_ms=(perf_counter() - started) * 1_000,
        )


class ContactQueryTool:
    name = "contact_query"
    description = (
        "按名称或别名查找用户的联系人上下文（关系、时区及当地当前时间、重要日期与"
        "倒计时、授权偏好）。用户提到某个人并需要背景时调用；找不到就明确说没有"
        "记录，不要编造联系人信息。"
    )
    arguments_model: type[BaseModel] = ContactQueryArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    def __init__(
        self, store: ContactStore, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=ContactQueryArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(ContactQueryArgs, arguments)
        if context.privacy_level == PrivacyLevel.L2:
            return ToolResult(
                ok=False,
                tool_name=self.name,
                reason_code="private_session_unsupported",
                latency_ms=(perf_counter() - started) * 1_000,
            )
        if context.user_id is None:
            return ToolResult(
                ok=False,
                tool_name=self.name,
                reason_code="user_missing",
                latency_ms=(perf_counter() - started) * 1_000,
            )
        matches = await self._store.list_contacts(context.user_id, query=args.name, limit=5)
        now = self._clock()
        today = now.astimezone(ZoneInfo("UTC")).date()
        return ToolResult(
            ok=True,
            tool_name=self.name,
            data={
                "query": args.name,
                "found": bool(matches),
                "contacts": [_contact_payload(view, today=today) for view in matches],
            },
            latency_ms=(perf_counter() - started) * 1_000,
        )


def _contact_payload(view: ContactView, *, today: date | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": str(view.id),
        "display_name": view.display_name,
        "aliases": list(view.aliases),
        "relationship": view.relationship,
        "timezone": view.timezone,
        "important_dates": [item.model_dump(mode="json") for item in view.important_dates],
        "preferences": [item.model_dump(mode="json") for item in view.preferences],
        "notes": view.notes,
    }
    if view.timezone is not None:
        now = datetime.now(ZoneInfo(view.timezone))
        payload["local_time"] = now.strftime("%Y-%m-%d %H:%M")
    if today is not None:
        countdowns = []
        for item in view.important_dates:
            remaining = days_until(today, item.month, item.day)
            if remaining is not None:
                countdowns.append({"label": item.label, "days_until": remaining})
        payload["date_countdowns"] = countdowns
    return payload
