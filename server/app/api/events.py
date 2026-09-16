from __future__ import annotations

import hmac
import os
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, HTTPException, status
from pydantic import Field

from app.api.admin_config import _runtime_admin_token
from app.bus import append_event
from app.db import Database
from app.schemas import InputEnvelope
from app.schemas.common import StrictModel


class EventSubmission(StrictModel):
    event: InputEnvelope
    topics: Annotated[list[str], Field(min_length=1, max_length=16)]


class EventSubmissionResult(StrictModel):
    accepted: bool
    event_id: str


def create_event_router(database: Database) -> APIRouter:
    router = APIRouter(prefix="/api/v1/dev", tags=["development"])

    async def _guard(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        """配置了管理令牌时强制 Bearer 鉴权；纯本地开发（无令牌）才放行。

        事件注入可伪造感知输入（假"到家"触发场景计划、污染记忆），不能在
        有令牌的生产部署里保持无鉴权。
        """
        token = _runtime_admin_token or os.getenv("ARIA_ADMIN_TOKEN")
        if not token:
            return
        scheme, separator, credential = (authorization or "").partition(" ")
        if (
            separator != " "
            or scheme.casefold() != "bearer"
            or not hmac.compare_digest(credential.strip(), token)
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="admin credential required")

    router.dependencies.append(Depends(_guard))

    @router.post(
        "/events",
        response_model=EventSubmissionResult,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def submit_event(
        submission: Annotated[EventSubmission, Body()],
    ) -> EventSubmissionResult:
        async with database.sessions.begin() as session:
            accepted = await append_event(
                session,
                submission.event,
                topics=submission.topics,
            )
        return EventSubmissionResult(
            accepted=accepted,
            event_id=str(submission.event.event_id),
        )

    return router
