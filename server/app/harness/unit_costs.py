"""Explicit unit quotes and quantities; estimates are never provider invoices."""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Annotated, Literal

from pydantic import Field, StrictBool, model_validator

from app.schemas.common import StrictModel

CostUnit = Literal["request", "image", "second", "character", "byte"]
MAX_COST_MICROS = (1 << 63) - 1
Quantity = Annotated[Decimal, Field(ge=0, le=1_000_000_000, max_digits=24, decimal_places=12)]


class UnitPricing(StrictModel):
    unit: CostUnit
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    rate_per_unit: Annotated[
        Decimal, Field(ge=0, le=1_000_000_000, max_digits=24, decimal_places=12)
    ]


class UnitCostQuote(StrictModel):
    pricing: UnitPricing
    maximum_quantity: Quantity

    @model_validator(mode="after")
    def valid_reservation(self) -> UnitCostQuote:
        if self.maximum_quantity <= 0:
            raise ValueError("unit_cost_quantity_must_be_positive")
        if self.pricing.unit != "second" and self.maximum_quantity % 1:
            raise ValueError("unit_cost_quantity_must_be_integral")
        if unit_charge(self.pricing.rate_per_unit, self.maximum_quantity) > MAX_COST_MICROS:
            raise ValueError("unit_cost_reservation_overflow")
        return self


class UnitUsage(StrictModel):
    quantity: Quantity
    usage_known: StrictBool
    provider_request_id: Annotated[str, Field(min_length=1, max_length=200)] | None = None


def unit_charge(rate_per_unit: Decimal, quantity: Decimal) -> int:
    with localcontext() as context:
        context.prec = 64
        return int((rate_per_unit * quantity * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
