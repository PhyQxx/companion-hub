"""用户确认邮件：仅会话鉴权 HTTP 入口可以触发 SMTP，不暴露为模型工具。

附件上传/列表/丢弃同样走会话鉴权；文件内容只在确认发送时进入 MIME。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import Field

from app.auth import AuthService, ChatPrincipal
from app.db import MailAttachmentRecord
from app.mail import MailAttachmentStore, MailError, MailSendTool
from app.schemas.common import StrictModel

from .auth import ChatSessionGuard


class MailConfirmation(StrictModel):
    digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class MailAttachmentView(StrictModel):
    id: UUID
    filename: str
    mime_type: str
    size_bytes: int
    expires_at: str


def _attachment_view(record: MailAttachmentRecord) -> MailAttachmentView:
    return MailAttachmentView(
        id=record.id,
        filename=record.filename,
        mime_type=record.mime_type,
        size_bytes=record.size_bytes,
        expires_at=record.expires_at.isoformat(),
    )


def create_mail_router(
    tool: MailSendTool,
    auth_service: AuthService,
    *,
    attachments: MailAttachmentStore | None = None,
) -> APIRouter:
    guard = ChatSessionGuard(auth_service)
    router = APIRouter(prefix="/api/v1/mail", tags=["mail"])

    @router.get("/drafts")
    async def list_drafts(
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> list[dict[str, object]]:
        return await tool.list_drafts(principal.user_id)

    @router.post("/drafts/{draft_id}/confirm")
    async def confirm(
        draft_id: UUID,
        body: MailConfirmation,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> dict[str, object]:
        try:
            return await tool.confirm(principal.user_id, draft_id, body.digest)
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        except MailError as error:
            raise HTTPException(502, "mail_send_outcome_unknown") from error

    @router.post("/drafts/{draft_id}/cancel")
    async def cancel(
        draft_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> dict[str, object]:
        try:
            return await tool.cancel(principal.user_id, draft_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    if attachments is not None:

        @router.post("/attachments", status_code=201)
        async def upload_attachment(
            file: UploadFile,
            principal: Annotated[ChatPrincipal, Depends(guard)],
        ) -> MailAttachmentView:
            payload = await file.read(11 * 1024 * 1024)  # 10 MiB 上限 + 1 字节探测
            if len(payload) > 10 * 1024 * 1024:
                raise HTTPException(413, "attachment_too_large")
            try:
                record = await attachments.create(
                    user_id=principal.user_id,
                    filename=file.filename or "attachment",
                    mime_type=file.content_type or "",
                    payload=payload,
                )
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
            return _attachment_view(record)

        @router.get("/attachments")
        async def list_attachments(
            principal: Annotated[ChatPrincipal, Depends(guard)],
        ) -> list[MailAttachmentView]:
            return [
                _attachment_view(record)
                for record in await attachments.list_pending(principal.user_id)
            ]

        @router.delete("/attachments/{attachment_id}", status_code=204)
        async def discard_attachment(
            attachment_id: UUID,
            principal: Annotated[ChatPrincipal, Depends(guard)],
        ) -> None:
            await attachments.discard(principal.user_id, attachment_id)

    return router
