"""Explicit unit estimates cannot fabricate receipts, free usage or ledger ownership."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.harness.unit_costs import UnitCostQuote, UnitPricing, UnitUsage, unit_charge


@pytest.mark.parametrize("unit", ["request", "image", "second", "character", "byte"])
def test_declared_units_round_up_without_float(unit: str) -> None:
    quote = UnitCostQuote.model_validate(
        {
            "pricing": {"unit": unit, "currency": "CNY", "rate_per_unit": "0.000000000001"},
            "maximum_quantity": "3",
        }
    )
    assert unit_charge(quote.pricing.rate_per_unit, quote.maximum_quantity) == 1
    assert UnitCostQuote.model_validate_json(quote.model_dump_json()) == quote


def test_product_keeps_low_digits_beyond_default_decimal_context() -> None:
    rate, quantity = Decimal("999999999.000000000001"), Decimal("1.000000000001")
    assert unit_charge(rate, quantity) == 999999999001001
    assert unit_charge(Decimal("0"), Decimal("1000000000")) == 0


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1", "1000000001", "0.0000000000001"])
def test_bad_rates_and_quantities_are_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        UnitPricing(unit="second", currency="CNY", rate_per_unit=Decimal(bad))
    with pytest.raises(ValidationError):
        UnitUsage(quantity=Decimal(bad), usage_known=False)


@pytest.mark.parametrize(
    "unit,quantity",
    [
        ("request", "0"),
        ("image", "0.5"),
        ("character", "0.5"),
        ("byte", "0.5"),
        ("second", "1000000000"),
    ],
)
def test_invalid_quote_cannot_reserve(unit: str, quantity: str) -> None:
    with pytest.raises(ValidationError):
        UnitCostQuote.model_validate(
            {
                "pricing": {"unit": unit, "currency": "CNY", "rate_per_unit": "1000000000"},
                "maximum_quantity": quantity,
            }
        )


@pytest.mark.parametrize("currency", ["cny", "CN", "USDD", "", " CNY"])
def test_currency_must_be_declared(currency: str) -> None:
    with pytest.raises(ValidationError):
        UnitPricing(unit="request", currency=currency, rate_per_unit=Decimal(0))


@pytest.mark.parametrize("known", [None, "false", "true", 0, 1])
def test_unknown_and_known_zero_require_explicit_boolean(known: object) -> None:
    with pytest.raises(ValidationError):
        UnitUsage.model_validate({"quantity": "0", "usage_known": known})
    with pytest.raises(ValidationError):
        UnitUsage.model_validate({"quantity": "0"})
    assert UnitUsage(quantity=Decimal(0), usage_known=True).quantity == 0


def test_unit_is_explicit_and_unknown_fields_are_refused() -> None:
    with pytest.raises(ValidationError):
        UnitPricing.model_validate({"unit": "token", "currency": "CNY", "rate_per_unit": "1"})
    with pytest.raises(ValidationError):
        UnitUsage.model_validate({"quantity": "1", "usage_known": True, "cost_currency": "USD"})
