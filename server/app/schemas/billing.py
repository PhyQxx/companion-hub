"""Normalized operator-supplied billing evidence; no provider authenticity claim."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from .common import StrictModel, TokenName

Micros = Annotated[str, Field(pattern=r"^(0|[1-9]\d{0,18})$", max_length=19)]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Receipt = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[\x21-\x7e]+$")]


class BillLine(StrictModel):
    endpoint: TokenName
    provider_request_id: Receipt
    currency: Currency
    billed_micros: Micros


class CostPeriod(StrictModel):
    period_start: datetime
    period_end: datetime

    @model_validator(mode="after")
    def valid_period(self) -> Self:
        start, end = self.period_start, self.period_end
        if start.utcoffset() is None or end.utcoffset() is None:
            raise ValueError("billing_period_requires_timezone")
        if not timedelta(0) < end - start <= timedelta(days=366):
            raise ValueError("billing_period_invalid")
        object.__setattr__(self, "period_start", start.astimezone(UTC))
        object.__setattr__(self, "period_end", end.astimezone(UTC))
        return self


class BillingEvidence(CostPeriod):
    declared_complete: bool = False
    lines: list[BillLine] = Field(max_length=1000)


class LedgerLine(StrictModel):
    call_id: UUID
    endpoint: TokenName
    provider_request_id: Receipt | None
    currency: Currency | None
    charged_micros: Micros | None
    state: Literal["reserved", "estimated", "unknown"]


class CostSnapshot(CostPeriod):
    coverage: Literal["recorded_budgeted_model_calls"] = "recorded_budgeted_model_calls"
    lines: list[LedgerLine] = Field(max_length=10000)

    @model_validator(mode="after")
    def unique_calls(self) -> Self:
        if len({line.call_id for line in self.lines}) != len(self.lines):
            raise ValueError("duplicate_ledger_call")
        return self


ComparisonStatus = Literal[
    "matched",
    "amount_mismatch",
    "currency_mismatch",
    "ledger_unknown",
    "ambiguous_receipt",
    "missing_receipt",
    "ledger_only",
    "bill_only",
]


class BillingComparison(StrictModel):
    status: ComparisonStatus
    call_id: UUID | None = None
    receipt_sha256: str | None = None
    endpoint: str
    ledger_currency: str | None = None
    bill_currency: str | None = None
    held_micros: str | None = None
    billed_micros: str | None = None


class BillingReport(StrictModel):
    validation_level: Literal["V2"] = "V2"
    evidence: Literal["operator_supplied_bill_unverified"] = "operator_supplied_bill_unverified"
    budget_effect: Literal["none"] = "none"
    coverage: Literal["recorded_budgeted_model_calls"] = "recorded_budgeted_model_calls"
    period_start: datetime
    period_end: datetime
    bill_declared_complete: bool
    ledger_sha256: str
    bill_sha256: str
    counts: dict[str, int]
    comparisons: list[BillingComparison]
