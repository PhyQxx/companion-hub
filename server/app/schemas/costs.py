from datetime import datetime
from typing import Literal

from pydantic import Field

from .common import StrictModel


class CostCurrencyView(StrictModel):
    currency: str | None
    charged_micros: str = Field(pattern=r"^\d+$")
    estimated_calls: int
    reserved_calls: int
    unknown_calls: int
    unpriced_calls: int


class CostSummaryView(StrictModel):
    coverage: Literal["recorded_budgeted_model_calls"] = "recorded_budgeted_model_calls"
    validation: Literal["estimate_not_provider_bill"] = "estimate_not_provider_bill"
    timezone: Literal["UTC"] = "UTC"
    period_start: datetime
    period_end: datetime
    currencies: list[CostCurrencyView] = Field(default_factory=list)
