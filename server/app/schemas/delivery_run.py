from typing import Literal
from uuid import UUID

from pydantic import Field

from .common import StrictModel


class DeliveryRunOutcome(StrictModel):
    run_id: UUID
    status: Literal["not_started", "running", "returned", "not_delivered", "unknown", "cancelled"]
    validation_level: Literal["V0", "V1"] = "V0"
    channels: list[str] = Field(default_factory=list)
    reason_code: str | None = None
