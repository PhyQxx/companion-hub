"""Pure daily report projections; persistence and output live in adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from app.schemas.common import StrictModel
from app.schemas.delivery_run import DeliveryRunOutcome

ReviewSection = Literal["completed", "unfinished", "new_commitment", "tomorrow"]


class ReviewItem(StrictModel):
    section: ReviewSection
    text: Annotated[str, Field(min_length=1, max_length=280)]
    source: Annotated[str, Field(min_length=1, max_length=120)]
    action: Literal["pending", "confirmed", "removed"] = "pending"
    note: Annotated[str, Field(min_length=1, max_length=200)] | None = None


class ReviewView(StrictModel):
    delivery_outcome: DeliveryRunOutcome | None = None
    id: UUID
    user_id: UUID
    review_date: date
    items: list[ReviewItem]
    text: str
    status: Annotated[str, Field(pattern="^(pending|delivered)$")]
    delivered_at: datetime | None = None
    channels: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


ReviewDeliverer = Callable[..., Awaitable[list[str] | None]]


_SECTION_TITLES: dict[str, str] = {
    "completed": "完成事项",
    "unfinished": "未完成计划",
    "new_commitment": "新承诺",
    "tomorrow": "明日重点",
}
