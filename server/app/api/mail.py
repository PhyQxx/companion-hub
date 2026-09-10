"""用户确认邮件：仅会话鉴权 HTTP 入口可以触发 SMTP，不暴露为模型工具。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from app.auth import AuthService, ChatPrincipal
from app.mail import MailError, MailSendTool
from app.schemas.common import StrictModel

from .auth import ChatSessionGuard


class MailConfirmation(StrictModel):
    digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def create_mail_router(tool: MailSendTool, auth_service: AuthService) -> APIRouter:
    guard = ChatSessionGuard(auth_service)
    router = APIRouter(prefix="/api/v1/mail/drafts", tags=["mail"])

    @router.get("")
    async def list_drafts(
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> list[dict[str, object]]:
        return tool.list_drafts(principal.user_id)

    @router.post("/{draft_id}/confirm")
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

    @router.post("/{draft_id}/cancel")
    async def cancel(
        draft_id: UUID,
        principal: Annotated[ChatPrincipal, Depends(guard)],
    ) -> dict[str, object]:
        try:
            return tool.cancel(principal.user_id, draft_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    return router
