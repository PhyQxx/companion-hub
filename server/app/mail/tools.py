"""邮件读取与待发预览；发送只能由鉴权用户确认 API 触发。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import perf_counter
from typing import Annotated, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.config import ConfigStore, DatabaseConfigStore
from app.llm import ToolDefinition
from app.mail.client import MailClient, MailError, valid_address
from app.schemas.common import PrivacyLevel
from app.tools.contracts import ToolContext, ToolResult

_SUBJECT_LIMIT = 200
_BODY_LIMIT = 10_000


_AddressArg = Annotated[str, Field(min_length=3, max_length=254)]


class MailSendArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    to: Annotated[list[_AddressArg], Field(min_length=1, max_length=10)]
    cc: Annotated[list[_AddressArg], Field(max_length=10)] = Field(default_factory=list)
    subject: Annotated[str, Field(min_length=1, max_length=_SUBJECT_LIMIT)]
    body: Annotated[str, Field(min_length=1, max_length=_BODY_LIMIT)]
    confirmed: bool = False


class MailReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # 关键词命中主题/发件人/正文（IMAP TEXT 搜索）
    query: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    limit: Annotated[int, Field(ge=1, le=20)] = 10


@dataclass
class PendingMail:
    id: UUID
    user_id: UUID
    turn_id: UUID
    content: dict[str, object]
    digest: str
    expires_at: datetime
    status: str = "pending"
    receipt: dict[str, object] | None = None

    def view(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "content": self.content,
            "digest": self.digest,
            "expires_at": self.expires_at.isoformat(),
            "status": self.status,
            "receipt": self.receipt,
        }


class MailSendTool:
    name = "mail_send"
    description = (
        "准备待发送邮件，返回预览。用户必须在聊天界面的邮件卡片核对完整内容并点击"
        "确认发送；口头确认或 confirmed=true 不会发送。不要声称邮件已发出。"
        "收件人地址不全时先追问，不要猜。修改内容会生成新的待确认预览。"
    )
    arguments_model: type[BaseModel] = MailSendArgs
    max_privacy_level = PrivacyLevel.L1

    def __init__(self, client: MailClient, *, clock: Callable[[], datetime] | None = None) -> None:
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))
        # 短期内存预览；重启即失效，发送结果不明绝不自动重试。
        self._drafts: dict[UUID, PendingMail] = {}

    @property
    def available(self) -> bool:
        return self._client.is_configured()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=MailSendArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(MailSendArgs, arguments)
        if context.privacy_level != PrivacyLevel.L1:
            return self._failure("private_session_unsupported", started)
        if context.turn_id is None or context.user_id is None:
            return self._failure("idempotency_key_missing", started)
        if any(not valid_address(item) for item in [*args.to, *args.cc]):
            return self._failure("invalid_recipient", started)
        if args.confirmed:
            return self._failure("user_confirmation_required", started)
        content = args.model_dump(exclude={"confirmed"})
        digest = sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        now = self._clock()
        self._drafts = {
            key: draft
            for key, draft in self._drafts.items()
            if draft.expires_at > now or draft.status == "sending"
        }
        for draft in self._drafts.values():
            if draft.user_id == context.user_id and draft.turn_id == context.turn_id:
                if draft.digest == digest:
                    return self._preview(draft, started)
                if draft.status == "pending":
                    draft.status = "cancelled"
        if len(self._drafts) >= 256:
            return self._failure("mail_preview_capacity", started)
        draft = PendingMail(
            uuid4(),
            context.user_id,
            context.turn_id,
            content,
            digest,
            now + timedelta(minutes=15),
        )
        self._drafts[draft.id] = draft
        return self._preview(draft, started)

    def _preview(self, draft: PendingMail, started: float) -> ToolResult:
        return ToolResult(
            ok=True,
            tool_name=self.name,
            data={
                "sent": False,
                "confirmation_required": draft.status == "pending",
                "draft_id": str(draft.id),
                "status": draft.status,
                "preview": draft.content,
                "expires_at": draft.expires_at.isoformat(),
            },
            latency_ms=(perf_counter() - started) * 1000,
        )

    def list_drafts(self, user_id: UUID) -> list[dict[str, object]]:
        return [
            draft.view()
            for draft in self._drafts.values()
            if draft.user_id == user_id and draft.expires_at > self._clock()
        ]

    def _owned(self, user_id: UUID, draft_id: UUID) -> PendingMail:
        draft = self._drafts.get(draft_id)
        if draft is None or draft.user_id != user_id:
            raise LookupError("mail_preview_not_found")
        if draft.expires_at <= self._clock():
            raise ValueError("mail_preview_expired")
        return draft

    def cancel(self, user_id: UUID, draft_id: UUID) -> dict[str, object]:
        draft = self._owned(user_id, draft_id)
        if draft.status != "pending":
            raise ValueError("mail_preview_not_pending")
        draft.status = "cancelled"
        return draft.view()

    async def confirm(self, user_id: UUID, draft_id: UUID, digest: str) -> dict[str, object]:
        draft = self._owned(user_id, draft_id)
        if digest != draft.digest:
            raise ValueError("mail_preview_changed")
        if draft.status == "sent":
            return draft.view()
        if draft.status != "pending":
            raise ValueError("mail_preview_not_pending")
        # 无 await 的认领阻止同一进程并发重复发送；SMTP 前置发送中标记。
        draft.status = "sending"
        args = MailSendArgs.model_validate(draft.content)
        try:
            draft.receipt = await self._client.send(
                to=args.to,
                subject=args.subject,
                body=args.body,
                cc=args.cc or None,
            )
        except BaseException:
            # SMTP 超时/取消可能已被服务器接受，保持不确定终态而非允许重试。
            draft.status = "unknown_outcome"
            raise
        draft.status = "sent"
        return draft.view()

    def _failure(self, reason: str, started: float) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=self.name,
            reason_code=reason,
            latency_ms=(perf_counter() - started) * 1000,
        )


class MailReadTool:
    name = "mail_read"
    description = (
        "读取用户邮箱收件箱的最近邮件摘要（发件人、主题、时间、正文前 200 字）。"
        "query 为可选关键词（命中主题/发件人/正文）；仅 L1 可用。回答中不要展开"
        "全文，先给摘要列表，用户想看哪封再说明。"
    )
    arguments_model: type[BaseModel] = MailReadArgs
    max_privacy_level = PrivacyLevel.L1

    def __init__(self, client: MailClient) -> None:
        self._client = client

    @property
    def available(self) -> bool:
        return self._client.is_configured()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=MailReadArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(MailReadArgs, arguments)
        if context.privacy_level not in {PrivacyLevel.L0, PrivacyLevel.L1}:
            return self._failure("private_session_unsupported", started)
        try:
            summaries = await self._client.fetch_inbox(query=args.query, limit=args.limit)
        except MailError as error:
            return self._failure(error.reason_code, started)
        return ToolResult(
            ok=True,
            tool_name=self.name,
            data={
                "messages": [
                    {
                        "uid": item.uid,
                        "sender": item.sender,
                        "subject": item.subject,
                        "sent_at": item.sent_at.isoformat() if item.sent_at else None,
                        "snippet": item.snippet,
                    }
                    for item in summaries
                ],
                "count": len(summaries),
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


def create_mail_tools(
    config_store: ConfigStore | DatabaseConfigStore,
) -> list[MailSendTool | MailReadTool]:
    client = MailClient(config_store)
    return [MailReadTool(client), MailSendTool(client)]
