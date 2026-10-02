"""The offline workload must exercise and commit both configured chat paths."""

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
