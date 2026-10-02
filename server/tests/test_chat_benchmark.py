"""The offline workload must exercise and commit both configured chat paths."""

import pytest

from scripts.benchmark_chat_harness import benchmark


async def test_whole_chat_fixture_commits_both_budget_modes_with_concurrent_history() -> None:
    report = await benchmark(samples=2, concurrency=2, warmup=1)
    assert report["synthetic"] is True
    assert report["verified_commits"] == 6
    assert report["samples_per_mode"] == 2
    assert report["environment"]["database"] == "isolated file SQLite"
    assert "not a pre-Harness" in report["comparison"]
    for mode in ("budget_on", "budget_off", "paired_budget_increment"):
        assert len(report["raw_ms"][mode]) == 2
        assert report[mode]["p50_ms"] <= report[mode]["p95_ms"] <= report[mode]["p99_ms"]


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://fixture.invalid/production",
        "postgresql://fixture.invalid/fixture_test",
        "sqlite+aiosqlite:///fixture_test",
        "postgresql+asyncpg://fixture.invalid/",
    ],
)
async def test_postgres_benchmark_rejects_non_test_targets_before_opening(url: str) -> None:
    with pytest.raises(ValueError, match="dedicated _test database"):
        await benchmark(samples=2, concurrency=1, warmup=0, postgres_test_url=url)
