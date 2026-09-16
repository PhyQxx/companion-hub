"""邮件读取与待发预览；发送只能由鉴权用户确认 API 触发。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from time import perf_counter
from typing import Annotated, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.config import ConfigStore, DatabaseConfigStore
from app.confirmation import (
    DatabasePendingMutationStore,
    PendingMutation,
    PendingMutationStore,
)
from app.llm import ToolDefinition
from app.mail.attachments import MailAttachmentStore
from app.mail.client import MailAttachment, MailClient, MailError, valid_address
from app.schemas.common import PrivacyLevel
from app.tools.contracts import ToolContext, ToolResult

_SUBJECT_LIMIT = 200
_BODY_LIMIT = 10_000


_AddressArg = Annotated[str, Field(min_length=3, max_length=254)]

# 共享预览底座状态 → 邮件卡片状态词表（保持前端契约不变）
_MAIL_STATUS = {"saving": "sending", "completed": "sent"}


class MailSendArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    to: Annotated[list[_AddressArg], Field(min_length=1, max_length=10)]
    cc: Annotated[list[_AddressArg], Field(max_length=10)] = Field(default_factory=list)
    subject: Annotated[str, Field(min_length=1, max_length=_SUBJECT_LIMIT)]
    body: Annotated[str, Field(min_length=1, max_length=_BODY_LIMIT)]
    # 已上传待发送附件的 ID（最多 3 个），文件内容永不进入模型上下文
    attachment_ids: Annotated[list[UUID], Field(max_length=3)] = Field(default_factory=list)
    confirmed: bool = False


class MailReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # 关键词命中主题/发件人/正文（IMAP TEXT 搜索）
    query: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    limit: Annotated[int, Field(ge=1, le=20)] = 10
    unread_only: bool = False
    # 目标文件夹（默认收件箱；名字来自 mail_folders 结果）
    folder: Annotated[str, Field(min_length=1, max_length=120)] = "INBOX"


def _mail_status(status: str) -> str:
    return _MAIL_STATUS.get(status, status)


def _mail_view(item: PendingMutation) -> dict[str, object]:
    return {
        "id": str(item.id),
        "content": item.content,
        "digest": item.digest,
        "expires_at": item.expires_at.isoformat(),
        "status": _mail_status(item.status),
        "receipt": item.result,
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

    def __init__(
        self,
        client: MailClient,
        *,
        drafts: PendingMutationStore | DatabasePendingMutationStore | None = None,
        attachments: MailAttachmentStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._attachments = attachments
        self._clock = clock or (lambda: datetime.now(UTC))
        # 预览经共享确认底座；持久化存储时重启不失效，发送结果不明绝不自动重试。
        self._drafts: PendingMutationStore | DatabasePendingMutationStore = (
            drafts or PendingMutationStore(clock=clock)
        )

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
        attachment_infos: list[dict[str, object]] = []
        if args.attachment_ids:
            if self._attachments is None:
                return self._failure("attachments_unavailable", started)
            seen: list[UUID] = []
            for attachment_id in args.attachment_ids:
                if attachment_id in seen:
                    return self._failure("invalid_recipient", started)
                seen.append(attachment_id)
                try:
                    record = await self._attachments.get_owned(context.user_id, attachment_id)
                except LookupError:
                    return self._failure("attachment_not_found", started)
                except ValueError:
                    return self._failure("attachment_expired", started)
                attachment_infos.append(
                    {
                        "id": str(record.id),
                        "filename": record.filename,
                        "mime_type": record.mime_type,
                        "size_bytes": record.size_bytes,
                    }
                )
        content: dict[str, object] = args.model_dump(exclude={"confirmed"}, mode="json")
        content["attachments"] = attachment_infos
        try:
            draft = await self._drafts.prepare(
                user_id=context.user_id,
                turn_id=context.turn_id,
                kind="mail_send",
                content=dict(content),
                preview=dict(content),
                now=self._clock(),
            )
        except OverflowError:
            return self._failure("mail_preview_capacity", started)
        return self._preview(draft, started)

    def _preview(self, draft: PendingMutation, started: float) -> ToolResult:
        return ToolResult(
            ok=True,
            tool_name=self.name,
            data={
                "sent": False,
                "confirmation_required": draft.status == "pending",
                "draft_id": str(draft.id),
                "status": _mail_status(draft.status),
                "preview": draft.content,
                "expires_at": draft.expires_at.isoformat(),
            },
            latency_ms=(perf_counter() - started) * 1000,
        )

    async def list_drafts(self, user_id: UUID) -> list[dict[str, object]]:
        return [_mail_view(item) for item in await self._drafts.list(user_id)]

    async def cancel(self, user_id: UUID, draft_id: UUID) -> dict[str, object]:
        return _mail_view(await self._drafts.cancel(user_id, draft_id))

    async def confirm(self, user_id: UUID, draft_id: UUID, digest: str) -> dict[str, object]:
        draft = await self._drafts.claim(user_id, draft_id, digest)
        if draft.status == "completed":
            return _mail_view(draft)
        # content 里的 attachments 是预览元数据，不是模型参数
        args = MailSendArgs.model_validate(
            {key: value for key, value in draft.content.items() if key != "attachments"}
        )
        attachment_ids = [
            UUID(str(item["id"]))
            for item in cast(list[dict[str, object]], draft.content.get("attachments") or [])
        ]
        attachments: list[MailAttachment] = []
        for attachment_id in attachment_ids:
            if self._attachments is None:
                raise MailError("attachments_unavailable")
            record = await self._attachments.get_owned(user_id, attachment_id)
            attachments.append(
                MailAttachment(
                    filename=record.filename,
                    mime_type=record.mime_type,
                    payload=bytes(record.payload),
                )
            )
        try:
            receipt = await self._client.send(
                to=args.to,
                subject=args.subject,
                body=args.body,
                cc=args.cc or None,
                attachments=attachments or None,
            )
        except BaseException:
            # SMTP 超时/取消可能已被服务器接受，保持不确定终态而非允许重试。
            await self._drafts.mark_unknown(draft)
            raise
        if self._attachments is not None and attachment_ids:
            await self._attachments.mark_used(attachment_ids)
        return _mail_view(await self._drafts.complete(draft, dict(receipt)))

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
            summaries = await self._client.fetch_inbox(
                query=args.query,
                limit=args.limit,
                unread_only=args.unread_only,
                folder=args.folder,
            )
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
                        "unread": item.unread,
                    }
                    for item in summaries
                ],
                "count": len(summaries),
                "unread_count": sum(1 for item in summaries if item.unread),
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


class MailMarkArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    uid: Annotated[int, Field(ge=1)]
    read: bool = True
    folder: Annotated[str, Field(min_length=1, max_length=120)] = "INBOX"


class MailFoldersArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MailMoveArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    uid: Annotated[int, Field(ge=1)]
    to_folder: Annotated[str, Field(min_length=1, max_length=120)]
    folder: Annotated[str, Field(min_length=1, max_length=120)] = "INBOX"


class MailAttachmentsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MailMarkTool:
    name = "mail_mark"
    description = (
        "把指定邮件标记为已读或未读（uid 来自 mail_read 结果）。仅 L1 可用；"
        "用户明确要求标记时才调用。"
    )
    arguments_model: type[BaseModel] = MailMarkArgs
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
            parameters=MailMarkArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(MailMarkArgs, arguments)
        if context.privacy_level != PrivacyLevel.L1:
            return self._failure("private_session_unsupported", started)
        if context.user_id is None:
            return self._failure("idempotency_key_missing", started)
        try:
            ok = await self._client.mark_read(
                args.uid, read=args.read, folder=args.folder
            )
        except MailError as error:
            return self._failure(error.reason_code, started)
        return ToolResult(
            ok=ok,
            tool_name=self.name,
            data={"uid": args.uid, "read": args.read, "folder": args.folder}
            if ok
            else None,
            reason_code=None if ok else "mail_mark_failed",
            latency_ms=(perf_counter() - started) * 1_000,
        )

    def _failure(self, reason: str, started: float) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=self.name,
            reason_code=reason,
            latency_ms=(perf_counter() - started) * 1_000,
        )


class MailFoldersTool:
    name = "mail_folders"
    description = (
        "列出用户邮箱的可用文件夹名字（IMAP LIST）。mail_read/mail_mark/"
        "mail_move 的 folder 参数必须使用这里返回的名字，不要凭空猜测。仅 L1 可用。"
    )
    arguments_model: type[BaseModel] = MailFoldersArgs
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
            parameters=MailFoldersArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        if context.privacy_level != PrivacyLevel.L1:
            return self._failure("private_session_unsupported", started)
        try:
            folders = await self._client.list_folders()
        except MailError as error:
            return self._failure(error.reason_code, started)
        return ToolResult(
            ok=True,
            tool_name=self.name,
            data={"folders": folders, "count": len(folders)},
            latency_ms=(perf_counter() - started) * 1_000,
        )

    def _failure(self, reason: str, started: float) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=self.name,
            reason_code=reason,
            latency_ms=(perf_counter() - started) * 1_000,
        )


class MailMoveTool:
    name = "mail_move"
    description = (
        "把指定邮件移动到目标文件夹（uid 与源 folder 来自 mail_read 结果，"
        "目标文件夹名字来自 mail_folders）。用户明确要求归类时才调用；"
        "移动不可当场撤销，需用户自己移回。仅 L1 可用。"
    )
    arguments_model: type[BaseModel] = MailMoveArgs
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
            parameters=MailMoveArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(MailMoveArgs, arguments)
        if context.privacy_level != PrivacyLevel.L1:
            return self._failure("private_session_unsupported", started)
        if context.user_id is None:
            return self._failure("idempotency_key_missing", started)
        try:
            folders = await self._client.list_folders()
            if args.to_folder not in folders:
                return self._failure("folder_not_found", started)
            moved = await self._client.move_message(
                args.uid, to_folder=args.to_folder, folder=args.folder
            )
        except MailError as error:
            return self._failure(error.reason_code, started)
        return ToolResult(
            ok=moved,
            tool_name=self.name,
            data=(
                {"uid": args.uid, "to_folder": args.to_folder} if moved else None
            ),
            reason_code=None if moved else "mail_move_failed",
            latency_ms=(perf_counter() - started) * 1_000,
        )

    def _failure(self, reason: str, started: float) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=self.name,
            reason_code=reason,
            latency_ms=(perf_counter() - started) * 1_000,
        )


class MailAttachmentsTool:
    name = "mail_attachments"
    description = (
        "列出用户已上传、尚未随邮件发送的附件（id/文件名/大小）。用户要求把"
        "已上传的文件发给某人时，先用本工具拿到 attachment_id，再把 id 填进"
        " mail_send 的 attachment_ids。过期或已发送的附件不会出现。仅 L1 可用。"
    )
    arguments_model: type[BaseModel] = MailAttachmentsArgs
    max_privacy_level = PrivacyLevel.L1

    def __init__(self, attachments: MailAttachmentStore) -> None:
        self._attachments = attachments

    @property
    def available(self) -> bool:
        return True  # 本地存储，无需外部邮箱配置

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=MailAttachmentsArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        if context.privacy_level != PrivacyLevel.L1:
            return self._failure("private_session_unsupported", started)
        if context.user_id is None:
            return self._failure("idempotency_key_missing", started)
        records = await self._attachments.list_pending(context.user_id)
        return ToolResult(
            ok=True,
            tool_name=self.name,
            data={
                "attachments": [
                    {
                        "id": str(record.id),
                        "filename": record.filename,
                        "mime_type": record.mime_type,
                        "size_bytes": record.size_bytes,
                    }
                    for record in records
                ],
                "count": len(records),
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
    *,
    drafts: PendingMutationStore | DatabasePendingMutationStore | None = None,
    attachments: MailAttachmentStore | None = None,
) -> list[
    MailSendTool
    | MailReadTool
    | MailMarkTool
    | MailFoldersTool
    | MailMoveTool
    | MailAttachmentsTool
]:
    client = MailClient(config_store)
    tools: list[
        MailSendTool
        | MailReadTool
        | MailMarkTool
        | MailFoldersTool
        | MailMoveTool
        | MailAttachmentsTool
    ] = [
        MailReadTool(client),
        MailSendTool(client, drafts=drafts, attachments=attachments),
        MailMarkTool(client),
        MailFoldersTool(client),
        MailMoveTool(client),
    ]
    if attachments is not None:
        tools.append(MailAttachmentsTool(attachments))
    return tools
