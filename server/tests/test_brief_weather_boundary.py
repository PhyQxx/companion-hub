"""The configured weather adapter preserves budget denial and closes its runtime."""

import asyncio
from typing import Any, cast

import pytest

from app.harness.budget import BudgetDenied
from app.tasks import brief_sources
from app.tools.factory import QueryToolRuntime
from app.tools.location import ResolvedLocation


@pytest.mark.parametrize("stage", ["resolve", "live", "forecast"])
@pytest.mark.parametrize("failure", ["budget", "cancel", "ordinary"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_weather_adapter_preserves_terminal_failure_and_releases_runtime(
    stage: str,
    failure: str,
    cleanup_fails: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    error: BaseException = (
        BudgetDenied("synthetic_budget_denied")
        if failure == "budget"
        else asyncio.CancelledError()
        if failure == "cancel"
        else RuntimeError("fixture unavailable")
    )

    def check(name: str) -> None:
        calls.append(name)
        if name == stage:
            raise error

    class Runtime:
        @property
        def provider(self) -> object:
            return self

        async def weather(self, adcode: str, *, extensions: str) -> dict[str, object]:
            assert adcode == "000000"
            check("live" if extensions == "base" else "forecast")
            return {"lives": [{"weather": "sunny", "temperature": "20"}]}

        async def close(self) -> None:
            calls.append("close")
            if cleanup_fails:
                raise RuntimeError("fixture cleanup failed")

    async def resolve(provider: object, **kwargs: Any) -> ResolvedLocation:
        check("resolve")
        return ResolvedLocation("explicit", "fixture city", "000000", None, None)

    monkeypatch.setattr(brief_sources, "resolve_location", resolve)
    if failure == "ordinary" and cleanup_fails:
        with pytest.raises(RuntimeError, match="fixture cleanup failed"):
            await brief_sources.read_brief_weather(
                cast(QueryToolRuntime, Runtime()), city="fixture"
            )
    elif failure == "ordinary":
        assert (
            await brief_sources.read_brief_weather(
                cast(QueryToolRuntime, Runtime()), city="fixture"
            )
            is None
        )
    else:
        with pytest.raises(type(error)) as captured:
            await brief_sources.read_brief_weather(
                cast(QueryToolRuntime, Runtime()), city="fixture"
            )
        assert captured.value is error
    assert (
        calls
        == {
            "resolve": ["resolve", "close"],
            "live": ["resolve", "live", "close"],
            "forecast": ["resolve", "live", "forecast", "close"],
        }[stage]
    )
