"""Pure daily report projections; persistence and output live in adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from pydantic import Field

from app.schemas.common import StrictModel
from app.schemas.delivery_run import DeliveryRunOutcome


class BriefFact(StrictModel):
    kind: Annotated[str, Field(min_length=1, max_length=24)]
    text: Annotated[str, Field(min_length=1, max_length=240)]
    source: Annotated[str, Field(min_length=1, max_length=120)]


class BriefView(StrictModel):
    delivery_outcome: DeliveryRunOutcome | None = None
    id: UUID
    user_id: UUID
    brief_date: date
    facts: list[BriefFact]
    text: str
    status: Annotated[str, Field(pattern="^(pending|delivered)$")]
    delivered_at: datetime | None = None
    channels: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BriefCommute:
    """当日首个带地点日程的出行建议（由 CommuteService 只读计算）。"""

    destination: str
    leave_by: datetime
    event_title: str
    starts_at: datetime
    mode: str
    duration_min: int | None
    event_id: UUID | None = None
    user_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class BriefWeather:
    city: str
    condition: str
    temperature_c: str
    low_c: str | None
    high_c: str | None


BriefWeatherFetcher = Callable[[], Awaitable[BriefWeather | None]]


BriefDeliverer = Callable[..., Awaitable[list[str] | None]]


BriefCommuteFetcher = Callable[[UUID], Awaitable[BriefCommute | None]]
