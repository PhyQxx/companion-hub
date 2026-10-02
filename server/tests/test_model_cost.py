from time import perf_counter
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.chat.memory_consistency import _merge_usage
from app.ids import uuid7
from app.llm import (
    CompletionRequest,
    CompletionResult,
    EnvSecretProvider,
    LiteLLMProvider,
    ModelEndpoint,
)


def priced_result(
    *,
    rates: tuple[float | None, float | None] = (None, None),
    currency: str | None = None,
    usage: Any = None,
    name: str = "local",
) -> CompletionResult:
    endpoint = ModelEndpoint(
        provider="openai_compatible",
        model="fixture",
        base_url="http://127.0.0.1:1/v1",
        runs_local=True,
        max_privacy_level="L2",
        input_cost_per_million=rates[0],
        output_cost_per_million=rates[1],
        cost_currency=currency,
    )
    provider = LiteLLMProvider(name, endpoint, EnvSecretProvider({}))
    return provider._result(
        request=CompletionRequest(
            trace_id=uuid7(),
            messages=[{"role": "user", "content": "synthetic"}],
            privacy_level="L2",
            route="private",
        ),
        text="fixture",
        request_id="fixture",
        finish_reason="stop",
        usage=usage,
        started=perf_counter(),
    )


@pytest.mark.parametrize("rates", [(None, None), (1.0, None), (None, 2.0)])
def test_missing_price_is_unknown_even_for_local_endpoint(
    rates: tuple[float | None, float | None],
) -> None:
    result = priced_result(rates=rates, usage=SimpleNamespace(prompt_tokens=4, completion_tokens=3))
    assert result.usage.estimated_cost is None
    assert result.usage.model_dump()["estimated_cost"] is None


@pytest.mark.parametrize(
    "usage",
    [
        None,
        SimpleNamespace(total_tokens=7),
        SimpleNamespace(prompt_tokens=4),
        SimpleNamespace(prompt_tokens=4, completion_tokens=3, total_tokens=9),
    ],
)
def test_missing_or_inconsistent_split_does_not_invent_cost(usage: Any) -> None:
    assert priced_result(rates=(1, 2), currency="CNY", usage=usage).usage.estimated_cost is None


def test_explicit_free_and_known_price_are_distinct_from_missing() -> None:
    usage = SimpleNamespace(prompt_tokens=4, completion_tokens=3, total_tokens=7)
    assert priced_result(rates=(0, 0), usage=usage).usage.estimated_cost == 0
    result = priced_result(rates=(1, 2), currency="CNY", usage=usage)
    assert result.usage.estimated_cost == pytest.approx(0.00001)
    assert result.usage.cost_currency == "CNY"


@pytest.mark.parametrize("currency", ["USD", None])
def test_repair_does_not_sum_different_or_unspecified_currency(currency: str | None) -> None:
    usage = SimpleNamespace(prompt_tokens=4, completion_tokens=3)
    first = priced_result(rates=(1, 2), currency="CNY", usage=usage)
    repair = priced_result(rates=(1, 2), currency=currency, usage=usage, name="fallback")
    combined = _merge_usage(first, repair)
    assert combined.usage.estimated_cost is None
    assert combined.usage.cost_currency is None
    assert combined.usage.total_tokens == 14


def test_repair_preserves_unknown_usage_and_aggregates_only_same_currency() -> None:
    usage = SimpleNamespace(prompt_tokens=4, completion_tokens=3)
    first = priced_result(rates=(1, 2), currency="CNY", usage=usage)
    repair = priced_result(rates=(1, 2), currency="CNY", usage=usage, name="fallback")
    assert _merge_usage(first, repair).usage.estimated_cost == pytest.approx(0.00002)
    unknown = priced_result(rates=(1, 2), currency="CNY")
    combined = _merge_usage(first, unknown)
    assert combined.usage.estimated_cost is None
    assert combined.usage.usage_known is False


@pytest.mark.parametrize("rate", [-1, float("inf"), float("nan")])
def test_rates_must_be_finite_nonnegative(rate: float) -> None:
    with pytest.raises(ValidationError):
        priced_result(rates=(rate, 0))
