"""Benchmark provenance includes runtime dependencies and refuses mixed sources."""

import json
from pathlib import Path

import pytest

from scripts import benchmark_chat_harness as chat
from scripts import benchmark_perception_admission as perception
from scripts.benchmark_storage import (
    FixtureStorage,
    SourceProvenance,
    open_storage,
    source_provenance,
)


def tree(root: Path) -> None:
    (root / "app").mkdir()
    (root / "scripts").mkdir()
    (root / "app" / "fixture.py").write_text("synthetic private fixture body")
    (root / "scripts" / "fixture.py").write_text("synthetic benchmark")


def test_provenance_covers_named_sources_without_exporting_content(tmp_path: Path) -> None:
    tree(tmp_path)
    first = source_provenance(tmp_path)
    assert first.file_count == 2 and len(first.fingerprint) == 64
    assert source_provenance(tmp_path) == first
    (tmp_path / "app" / "ignore.json").write_text("synthetic private fixture body")
    assert source_provenance(tmp_path) == first
    assert "synthetic private fixture body" not in json.dumps(first.fields())


@pytest.mark.parametrize("change", ["content", "rename", "dependency"])
def test_provenance_detects_content_names_and_added_runtime_dependencies(
    tmp_path: Path, change: str
) -> None:
    tree(tmp_path)
    first = source_provenance(tmp_path)
    path = tmp_path / "app" / "fixture.py"
    if change == "content":
        path.write_text("changed synthetic source")
    elif change == "rename":
        path.rename(path.with_name("renamed.py"))
    else:
        (tmp_path / "app" / "dependency.py").write_text("added runtime dependency")
    assert source_provenance(tmp_path) != first


def test_provenance_declines_empty_source_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="benchmark_source_missing"):
        source_provenance(tmp_path)


@pytest.mark.parametrize("mode", ["chat", "perception"])
async def test_benchmark_source_drift_aborts_after_owned_storage_cleanup(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = chat if mode == "chat" else perception
    original = open_storage
    paths: list[Path] = []
    versions = [SourceProvenance("a" * 64, 1), SourceProvenance("b" * 64, 1)]

    async def storage(path: Path, postgres_test_url: str | None = None) -> FixtureStorage:
        paths.append(path)
        return await original(path, postgres_test_url)

    def changed() -> SourceProvenance:
        return versions.pop(0)

    monkeypatch.setattr(module, "open_storage", storage)
    monkeypatch.setattr(module, "source_provenance", changed)
    with pytest.raises(RuntimeError, match="benchmark_source_changed"):
        await module.benchmark(samples=2, concurrency=1, warmup=0)
    assert len(paths) == 2 and all(not path.exists() for path in paths)
    assert versions == []
