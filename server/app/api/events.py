from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, status
from pydantic import Field

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
