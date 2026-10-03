"""Check static import boundaries and an explicit, shrinking legacy allowlist.

Imports under TYPE_CHECKING are checked too. Relative imports are normalized;
new exceptions must be reviewed as architecture changes, not silently learned.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "server" / "app"
BASELINE = ROOT / "server" / "architecture" / "legacy_imports.json"
DOMAIN_PACKAGES = frozenset(
    {
        "memory",
        "persona",
        "cognition",
        "skills",
        "workflows",
        "calendar",
        "mail",
        "safety",
        "tasks",
        "contacts",
        "todo",
        "focus",
        "commute",
        "meetings",
        "perception",
    }
)
PURE_COGNITIVE_FLOW = frozenset(
    {
        "app.cognition.cycle",
        "app.cognition.ports",
        "app.cognition.world_assembly",
        "app.cognition.world_ports",
        "app.cognition.world_facts",
        "app.cognition.attention",
        "app.cognition.models",
        "app.cognition.proactive",
        "app.cognition.rule",
        "app.cognition.structured",
        "app.cognition.action",
        "app.cognition.reflection",
        "app.cognition.commitments",
        "app.cognition.commitment_ports",
        "app.memory.models",
        "app.memory.retrieval_models",
        "app.memory.retrieval",
        "app.memory.retrieval_ports",
        "app.memory.similarity",
        "app.memory.extraction_core",
        "app.memory.extraction_ports",
        "app.memory.extraction_rules",
        "app.memory.turn_core",
        "app.memory.consolidation_core",
        "app.memory.consolidation_ports",
        "app.meetings.models",
        "app.meetings.summary_core",
        "app.meetings.summary_ports",
        "app.timeline.models",
        "app.context.snapshots",
        "app.perception.pipeline",
        "app.perception.ports",
        "app.perception.models",
    }
)


def module_name(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(["app", *parts])


def imports(source: str, module: str, *, package: bool = False) -> Iterable[str]:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = module.split(".") if package else module.split(".")[:-1]
                base = base[: len(base) - node.level + 1]
                prefix = ".".join([*base, *([node.module] if node.module else [])])
            else:
                prefix = node.module or ""
            yield prefix
            # Covers `from app import api` and `from . import provider`.
            yield from (f"{prefix}.{alias.name}" for alias in node.names if alias.name != "*")


def allowed(source: str, target: str) -> bool:
    if source in PURE_COGNITIVE_FLOW and any(
        target == prefix or target.startswith(prefix + ".")
        for prefix in (
            "sqlalchemy",
            "sqlite3",
            "aiosqlite",
            "asyncpg",
            "httpx",
            "requests",
            "aiohttp",
            "openai",
            "anthropic",
        )
    ):
        return False
    if not target.startswith("app."):
        return True
    if source in PURE_COGNITIVE_FLOW:
        return any(
            target == prefix or target.startswith(prefix + ".")
            for prefix in (
                "app.cognition.models",
                "app.cognition.proactive",
                "app.cognition.rule",
                "app.cognition.ports",
                "app.cognition.commitment_ports",
                "app.cognition.attention",
                "app.cognition.world_facts",
                "app.cognition.world_ports",
                "app.context.snapshots",
                "app.memory.models",
                "app.memory.retrieval_models",
                "app.memory.retrieval",
                "app.memory.retrieval_ports",
                "app.memory.similarity",
                "app.memory.extraction_core",
                "app.memory.extraction_ports",
                "app.memory.extraction_rules",
                "app.memory.turn_core",
                "app.memory.consolidation_core",
                "app.memory.consolidation_ports",
                "app.meetings.models",
                "app.meetings.summary_core",
                "app.meetings.summary_ports",
                "app.timeline.models",
                "app.perception.models",
                "app.perception.ports",
                "app.harness",
                "app.schemas",
                "app.ids",
                "app.privacy.service",
            )
        )
    if source == "app.schemas" or source.startswith("app.schemas."):
        return target == "app.schemas" or target.startswith("app.schemas.")
    if source == "app.harness" or source.startswith("app.harness."):
        return any(
            target == prefix or target.startswith(prefix + ".")
            for prefix in ("app.harness", "app.schemas", "app.llm.contracts")
        )
    if source in {"app.db.base", "app.wiring.registry"}:
        return False
    package = source.split(".")[1] if "." in source else ""
    if package in DOMAIN_PACKAGES:
        return not any(
            target == prefix or target.startswith(prefix + ".")
            for prefix in ("app.api", "app.wiring", "app.main")
        )
    return True


def violations(root: Path = APP) -> set[str]:
    result: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        module = module_name(path, root)
        for target in imports(path.read_text(), module, package=path.name == "__init__.py"):
            if not allowed(module, target):
                result.add(f"{module} -> {target}")
    return result


def check(root: Path = APP, baseline: Path = BASELINE) -> list[str]:
    entries = json.loads(baseline.read_text())
    if not isinstance(entries, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("edge"), str)
        or not item.get("reason")
        or not item.get("retire_by")
        for item in entries
    ):
        raise ValueError("architecture_baseline_invalid")
    exceptions = {item["edge"] for item in entries}
    if len(exceptions) != len(entries):
        raise ValueError("architecture_baseline_duplicate")
    current = violations(root)
    return [
        *(f"new forbidden dependency: {edge}" for edge in sorted(current - exceptions)),
        *(f"remove retired exception: {edge}" for edge in sorted(exceptions - current)),
    ]


def main() -> int:
    failures = check()
    if failures:
        print("\n".join(failures))
        return 1
    print("Architecture boundaries passed; no new or stale legacy exceptions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
