"""Pure modules must import in a fresh process with SQL and SDKs unavailable."""

import ast
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "app.memory.models",
        "app.memory.retrieval",
        "app.memory.consolidation_core",
        "app.memory.extraction_core",
        "app.memory.turn_core",
        "app.timeline.recall",
        "app.timeline.screen_recall",
        "app.timeline.browser_recall",
        "app.meetings.summary_core",
        "app.cognition.cycle",
        "app.cognition.structured",
        "app.cognition.commitments",
        "app.perception.pipeline",
        "app.harness.context",
        "app.harness.loop",
        "app.llm.contracts",
        "app.memory",
        "app.timeline",
        "app.meetings",
        "app.cognition",
        "app.perception",
        "app.llm",
        "app.config",
        "app.voice",
        "app.config.models",
        "app.voice.contracts",
        "app.memory:MemoryEntry",
        "app.timeline:HistoryRecallService",
        "app.meetings:StructuredMeetingSummarizer",
        "app.cognition:CognitiveCycle",
        "app.perception:SemanticEventAuditView",
        "app.llm:CompletionRequest",
        "app.config:HubConfig",
        "app.voice:SpeechRecognizer",
        "app.focus",
        "app.focus.analysis",
        "app.focus.core",
        "app.focus:FocusSessionService",
        "app.commute.service",
        "app.commute.ports",
        "app.calendar.models",
        "app.calendar.service_core",
        "app.calendar.ports",
        "app.calendar:CalendarCoordinator",
        "app.tasks.models",
        "app.contacts.models",
        "app.tasks.brief_models",
        "app.tasks.review_models",
        "app.tasks.report_ports",
        "app.tasks.report_rules",
        "app.tasks.brief_core",
        "app.tasks.review_core",
        "app.contacts",
        "app.contacts:ContactView",
        "app.tools.amap_models",
        "app.tools.route_parser",
        "app.commute:CommuteService",
        "app.tools:AmapProviderError",
        "app.calendar",
        "app.tasks",
        "app.tools",
        "app.commute",
        "app.meetings.service_core",
        "app.meetings.service_ports",
        "app.meetings:MeetingCoordinator",
    ],
)
def test_pure_import_without_persistence_or_provider_dependencies(module: str) -> None:
    root = Path(__file__).resolve().parents[1]
    program = """
import importlib
import importlib.abc
import sys

class Fence(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'sqlalchemy', 'httpx', 'requests', 'openai', 'anthropic',
                                     'asyncpg', 'aiosqlite', 'litellm'}:
            raise AssertionError('Pure import attempted adapter dependency: ' + fullname)

sys.meta_path.insert(0, Fence())
module, separator, symbol = sys.argv[1].partition(':')
package = importlib.import_module(module)
if separator:
    getattr(package, symbol)
"""
    result = subprocess.run(
        [sys.executable, "-c", program, module],
        cwd=root,
        env=os.environ | {"PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "name",
    [
        "memory",
        "timeline",
        "meetings",
        "cognition",
        "perception",
        "llm",
        "config",
        "voice",
        "focus",
        "calendar",
        "tasks",
        "tools",
        "commute",
        "contacts",
    ],
)
def test_all_legacy_exports_preserve_object_identity_caching_and_discovery(name: str) -> None:
    package = importlib.import_module(f"app.{name}")
    assert package.__file__ is not None
    tree = ast.parse(Path(package.__file__).read_text())
    declared: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = f"app.{name}.{node.module}" if node.level else node.module
        assert module is not None
        for alias in node.names:
            exported = alias.asname or alias.name
            if exported in package.__all__:
                declared[exported] = (module, alias.name)
    assert set(declared) == set(package.__all__) == set(package._EXPORTS)
    assert set(package.__all__) <= set(dir(package))
    for exported, (module, attribute) in declared.items():
        expected = getattr(importlib.import_module(module), attribute)
        assert getattr(package, exported) is expected
        assert package.__dict__[exported] is expected
    with pytest.raises(AttributeError, match="undefined_fixture"):
        _ = package.undefined_fixture
    assert "undefined_fixture" not in package.__dict__
