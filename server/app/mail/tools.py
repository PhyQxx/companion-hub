"""MAIL-01 聊天工具：只读收件摘要与两段式确认发送。

验收「收件人、主题必须显式确认」：mail_send 首次调用不携带 confirmed，
只返回待发内容预览；模型向用户复述（收件人/主题/正文摘要），用户明确
同意后再带 confirmed=true 发送。发送经 SMTP 出站，仅 L1 挂载（L2 私密
会话内容禁止外发邮件）。同回合按 turn 幂等，重复调用不重复发送。
"""

from __future__ import annotations

from collections import OrderedDict
from time import perf_counter
from typing import Annotated, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.config import ConfigStore, DatabaseConfigStore
from app.llm import ToolDefinition
from app.mail.client import MailClient, MailError, valid_address
from app.schemas.common import PrivacyLevel
from app.tools.contracts import ToolContext, ToolResult

_SUBJECT_LIMIT = 200
_BODY_LIMIT = 10_000
_SNIPPET_LIMIT = 120


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


class MailSendTool:
    name = "mail_send"
    description = (
        "经用户邮箱发送邮件（仅 L1）。首次调用不要传 confirmed 或传 false：工具只返回"
        "待发预览，必须把收件人、抄送、主题与正文内容完整复述给用户，用户明确同意后再带"
        "confirmed=true 发送；收件人地址不全时先追问，不要猜。发送成功返回 message_id。"
    )
    arguments_model: type[BaseModel] = MailSendArgs
    max_privacy_level = PrivacyLevel.L1

    def __init__(
        self,
        client: MailClient,
    ) -> None:
        self._client = client
        # 同回合幂等：确认阶段重放预览、发送后重放回执，绝不重复投递
        self._sent_by_turn: OrderedDict[UUID, dict[str, object]] = OrderedDict()

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
        if context.privacy_level not in {PrivacyLevel.L0, PrivacyLevel.L1}:
            return self._failure("private_session_unsupported", started)
        if context.turn_id is None:
            return self._failure("idempotency_key_missing", started)
        invalid = [item for item in [*args.to, *args.cc] if not valid_address(item)]
        if invalid:
            return self._failure("invalid_recipient", started)
        sent = self._sent_by_turn.get(context.turn_id)
        if sent is not None:
            return ToolResult(
                ok=True,
                tool_name=self.name,
                data={"sent": False, "duplicate": True, **sent},
                latency_ms=(perf_counter() - started) * 1_000,
            )
        if not args.confirmed:
            return ToolResult(
                ok=True,
                tool_name=self.name,
                data={
                    "sent": False,
                    "confirmation_required": True,
                    "preview": {
                        "to": args.to,
                        "cc": args.cc,
                        "subject": args.subject,
                        "body_chars": len(args.body),
                        "body_preview": args.body[:_SNIPPET_LIMIT],
                    },
                },
                latency_ms=(perf_counter() - started) * 1_000,
            )
        try:
            receipt = await self._client.send(
                to=args.to, subject=args.subject, body=args.body, cc=args.cc or None
            )
        except MailError as error:
            return self._failure(error.reason_code, started)
        self._remember(context.turn_id, receipt)
        return ToolResult(
            ok=True,
            tool_name=self.name,
            data={"sent": True, **receipt},
            latency_ms=(perf_counter() - started) * 1_000,
        )

    def _remember(self, turn_id: UUID, receipt: dict[str, object]) -> None:
        self._sent_by_turn[turn_id] = receipt
        while len(self._sent_by_turn) > 64:
            self._sent_by_turn.popitem(last=False)

    def _failure(self, reason: str, started: float) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=self.name,
            reason_code=reason,
            latency_ms=(perf_counter() - started) * 1_000,
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
