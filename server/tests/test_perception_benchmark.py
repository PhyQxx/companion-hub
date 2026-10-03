"""Benchmark verifies committed work and cleans storage on partial failure."""

from pathlib import Path
from typing import Any

import pytest

from scripts import benchmark_perception_admission as benchmark
from scripts.benchmark_storage import FixtureStorage


@pytest.mark.parametrize("invalid", ["dimensions", "url"])
async def test_invalid_perception_benchmark_refuses_storage_before_creation(
    monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> FixtureStorage:
        raise AssertionError("invalid benchmark opened storage")

    monkeypatch.setattr(benchmark, "open_storage", forbidden)
    with pytest.raises(ValueError):
        await benchmark.benchmark(
            0 if invalid == "dimensions" else 1,
            1,
            0,
            None if invalid == "dimensions" else "postgresql+asyncpg://localhost/production",
        )


@pytest.mark.parametrize("failure", [False, True])
async def test_perception_benchmark_verifies_commits_and_closes_each_fixture(
    monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    original_close, original_case = FixtureStorage.close, benchmark.run_case
    closed: list[Path] = []
    calls = 0

    async def close(storage: FixtureStorage) -> None:
        assert storage.database.engine.url.database is not None
        path = Path(storage.database.engine.url.database)
        await original_close(storage)
        closed.append(path)

    async def run_case(storage: FixtureStorage, **kwargs: Any) -> list[float]:
        nonlocal calls
        calls += 1
        if failure and calls == 2:
            raise RuntimeError("synthetic second-phase failure")
        return await original_case(storage, **kwargs)

    monkeypatch.setattr(FixtureStorage, "close", close)
    monkeypatch.setattr(benchmark, "run_case", run_case)
    if failure:
        with pytest.raises(RuntimeError, match="synthetic second-phase failure"):
            await benchmark.benchmark(3, 2, 1)
    else:
        result = await benchmark.benchmark(3, 2, 1)
        assert result["verified_decisions"] == 8 and result["synthetic"] is True
        assert len(result["source_fingerprint"]) == 64
        assert all(len(values) == 3 for values in result["raw_ms"].values())
    assert len(closed) == 2 and all(not path.exists() for path in closed)
