"""Regression invariants for the first architecture migration slice."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from app.db import Base
from app.db.base import Base as SharedBase
from app.db.models import Base as LegacyBase
from app.db.registry import registered_metadata
from app.harness.context import ContextAssembler, ContextBlocks
from app.screen_awareness import ScreenAwarenessLoop
from app.skills.connections import SkillHttpClient
from app.wiring.lifespan import LifespanDeps, build_lifespan
from app.wiring.registry import ModuleRegistry, ModuleSpec
from scripts import check_architecture

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "module",
    ["app.chat", "app.cognition", "app.home_assistant", "app.perception", "app.output"],
)
def test_domain_entry_imports_without_prior_package_initialization(module: str) -> None:
    # An already-running pytest process hides order-sensitive package cycles.
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import importlib, sys; sys.path.insert(0, sys.argv[1]); "
            "importlib.import_module(sys.argv[2])",
            str(ROOT / "server"),
            module,
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )


@dataclass(frozen=True)
class Message:
    seq: int
    role: str
    content: str


def test_context_preserves_grounding_order_and_filters_non_dialogue_messages() -> None:
    history = [
        Message(12, "user", "question"),
        Message(13, "assistant", "answer"),
        Message(14, "system", "must not become a second system instruction"),
    ]
    result = ContextAssembler().assemble(
        system_prompt="persona",
        reply_instruction="reply contract",
        history=history,
        conversation_summary=("previous facts", 11),
        blocks=ContextBlocks(
            time="clock",
            reality="capabilities",
            action_catalog="actions",
            recent_device="device",
            memory="facts",
            screen_activity="screen",
            browser_activity="browser",
            history_recall="sources",
            skill_guidance="skill",
            unavailable_capability="unavailable",
        ),
    )
    assert result[0].content == (
        "personareply contract\n\nclock\n\ncapabilities\n\nactions\n\ndevice\n\nfacts"
        "\n\nscreen\n\nbrowser\n\nsources\n\n【此前对话要点（截至第 11 条消息）】"
        "\nprevious facts\n\nskill\n\nunavailable"
    )
    assert [(message.role, message.content) for message in result[1:]] == [
        ("user", "question"),
        ("assistant", "answer"),
    ]


@pytest.mark.parametrize(
    ("start", "watermark", "present"),
    [(1, 20, False), (12, 10, False), (12, 11, True), (12, 12, True)],
)
def test_summary_requires_watermark_coverage(start: int, watermark: int, present: bool) -> None:
    value = ContextAssembler.summary_block([Message(start, "user", "q")], ("summary", watermark))
    assert bool(value) is present
    assert ContextAssembler.summary_block([], ("summary", watermark)) == ""


def test_orm_mapping_identity_and_table_set_are_unchanged() -> None:
    assert Base is SharedBase is LegacyBase
    metadata = registered_metadata()
    assert metadata is Base.metadata
    assert sorted(metadata.tables) == json.loads(
        (ROOT / "server" / "architecture" / "mapped_tables.json").read_text()
    )
    assert importlib.util.find_spec("app.db.models") is not None


def test_architecture_rejects_relative_and_type_checking_imports(tmp_path: Path) -> None:
    root = tmp_path / "app"
    folder = root / "harness"
    folder.mkdir(parents=True)
    (folder / "context.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from ..db import Database\n"
        "from .. import api\nfrom app.llm.contracts import LLMMessage\n"
    )
    baseline = tmp_path / "baseline.json"
    baseline.write_text("[]")
    failures = check_architecture.check(root, baseline)
    assert any("app.harness.context -> app.db" in failure for failure in failures)
    assert any("app.harness.context -> app.api" in failure for failure in failures)
    assert not any("llm.contracts" in failure for failure in failures)
    (folder / "context.py").write_text("from app.schemas import PrivacyLevel\n")
    assert check_architecture.check(root, baseline) == []
    baseline.write_text(
        json.dumps(
            [
                {
                    "edge": "app.harness.context -> app.db",
                    "reason": "legacy",
                    "retire_by": "A",
                }
            ]
        )
    )
    assert check_architecture.check(root, baseline) == [
        "remove retired exception: app.harness.context -> app.db",
    ]


def test_project_architecture_boundaries() -> None:
    assert check_architecture.check() == []


@pytest.mark.parametrize("module", ["app.cognition.cycle", "app.perception.pipeline"])
@pytest.mark.parametrize(
    "dependency", ["app.db", "app.perception.admission", "app.cognition.store"]
)
def test_cognitive_flow_rejects_concrete_adapters(module: str, dependency: str) -> None:
    assert not check_architecture.allowed(module, dependency)
    assert check_architecture.allowed(module, "app.cognition.ports")
    assert check_architecture.allowed(module, "app.perception.ports")


@pytest.mark.parametrize(
    "specs",
    [
        [ModuleSpec("a", requires=("missing",))],
        [ModuleSpec("a", requires=("b",)), ModuleSpec("b", requires=("a",))],
        [ModuleSpec("a"), ModuleSpec("a")],
        [ModuleSpec("a", provides=("same",)), ModuleSpec("b", provides=("same",))],
    ],
)
def test_module_registration_rejects_invalid_graph(specs: list[ModuleSpec]) -> None:
    with pytest.raises(ValueError):
        ModuleRegistry(specs)


async def test_module_lifecycle_is_ordered_and_idempotent() -> None:
    calls: list[str] = []

    async def start_base() -> None:
        calls.append("base.start")

    async def stop_base() -> None:
        calls.append("base.stop")

    async def start_app() -> None:
        calls.append("app.start")

    async def stop_app() -> None:
        calls.append("app.stop")

    registry = ModuleRegistry(
        [
            ModuleSpec("app", requires=("base",), start=start_app, stop=stop_app),
            ModuleSpec("base", start=start_base, stop=stop_base),
        ]
    )
    await registry.start_all()
    await registry.start_all()
    assert registry.order == ("base", "app")
    await registry.stop_all()
    await registry.stop_all()
    assert calls == ["base.start", "app.start", "app.stop", "base.stop"]


async def test_optional_failure_blocks_dependents_and_critical_failure_rolls_back() -> None:
    stopped: list[str] = []

    async def fail() -> None:
        raise RuntimeError("failure")

    async def cleanup() -> None:
        stopped.append("cleanup")

    registry = ModuleRegistry(
        [
            ModuleSpec("base", stop=cleanup),
            ModuleSpec("optional", critical=False, start=fail, stop=cleanup),
            ModuleSpec("dependent", critical=False, requires=("optional",)),
            ModuleSpec("critical", start=fail, stop=cleanup),
        ]
    )
    with pytest.raises(RuntimeError, match="failure"):
        await registry.start_all()
    assert registry.states["optional"] == "degraded"
    assert registry.states["dependent"] == "blocked"
    assert registry.states["base"] == "stopped"
    assert stopped == ["cleanup", "cleanup", "cleanup"]


async def test_shutdown_failure_still_releases_other_modules() -> None:
    calls: list[str] = []

    async def fail() -> None:
        calls.append("fail")
        raise RuntimeError("shutdown")

    async def stop() -> None:
        calls.append("stop")

    registry = ModuleRegistry([ModuleSpec("base", stop=stop), ModuleSpec("app", stop=fail)])
    await registry.start_all()
    with pytest.raises(ExceptionGroup):
        await registry.stop_all()
    assert calls == ["fail", "stop"]
    await registry.stop_all()
    assert calls == ["fail", "stop"]


async def test_cancelled_start_cleans_partial_resources() -> None:
    cleanup: list[bool] = []

    async def cancelled() -> None:
        raise asyncio.CancelledError

    async def stop() -> None:
        cleanup.append(True)

    registry = ModuleRegistry([ModuleSpec("partial", start=cancelled, stop=stop)])
    with pytest.raises(asyncio.CancelledError):
        await registry.start_all()
    assert cleanup == [True]


async def test_lifespan_drains_conversation_before_closing_skill_client() -> None:
    from app.chat import ChatService

    calls: list[str] = []

    class Conversation:
        async def recover_incomplete_turns(self) -> None:
            calls.append("recover")

        async def drain_background_work(self) -> None:
            calls.append("drain")

    class Client:
        async def close(self) -> None:
            calls.append("close")

    app = FastAPI()
    deps = LifespanDeps(
        runtime_chat_service=cast(ChatService, Conversation()),
        skill_http_client=cast(SkillHttpClient, Client()),
    )
    async with build_lifespan(deps)(app):
        assert app.state.module_registry.states["conversation"] == "ready"
        assert calls == ["recover"]
    assert calls == ["recover", "drain", "close"]


@pytest.mark.parametrize("phase", ["startup", "shutdown"])
async def test_lifespan_failure_still_closes_migrated_clients(phase: str) -> None:
    from app.config import ConfigStore
    from app.observability.selfcheck import DailySelfCheckScheduler

    closed: list[bool] = []

    class Config:
        async def load(self) -> None:
            raise RuntimeError("startup")

    class Scheduler:
        def start(self) -> None:
            pass

        async def stop(self) -> None:
            raise RuntimeError("shutdown")

    class Client:
        async def close(self) -> None:
            closed.append(True)

    deps = LifespanDeps(
        runtime_config=cast(ConfigStore, Config()) if phase == "startup" else None,
        self_check_scheduler=cast(DailySelfCheckScheduler, Scheduler())
        if phase == "shutdown"
        else None,
        skill_http_client=cast(SkillHttpClient, Client()),
    )
    expected = RuntimeError if phase == "startup" else ExceptionGroup
    with pytest.raises(expected):
        async with build_lifespan(deps)(FastAPI()):
            assert phase == "shutdown"
    assert closed == [True]


async def test_optional_services_degrade_and_focus_is_owned_by_lifecycle() -> None:
    calls: list[str] = []

    class Optional:
        def start(self) -> None:
            calls.append("optional.start")
            raise RuntimeError("unavailable")

        async def stop(self) -> None:
            calls.append("optional.stop")

    class Focus:
        def start(self) -> None:
            calls.append("focus.start")

        async def stop(self) -> None:
            calls.append("focus.stop")

    app = FastAPI()
    deps = LifespanDeps(
        focus_scheduler=Focus(), screen_awareness_loop=cast(ScreenAwarenessLoop, Optional())
    )
    async with build_lifespan(deps)(app):
        assert app.state.module_registry.states["screen-awareness"] == "degraded"
        assert app.state.module_registry.states["focus"] == "ready"
        assert app.state.module_registry.reason_codes["screen-awareness"] == "module_start_failed"
    assert calls == ["optional.start", "optional.stop", "focus.start", "focus.stop"]


async def test_plan_reports_stop_before_conversation_drains() -> None:
    from app.chat import ChatService

    calls: list[str] = []

    class Conversation:
        async def recover_incomplete_turns(self) -> None:
            calls.append("conversation.start")

        async def drain_background_work(self) -> None:
            calls.append("conversation.stop")

    class Reports:
        def start(self) -> None:
            calls.append("reports.start")

        async def stop(self) -> None:
            calls.append("reports.stop")

    app = FastAPI()
    deps = LifespanDeps(
        runtime_chat_service=cast(ChatService, Conversation()), plan_completion_reporter=Reports()
    )
    async with build_lifespan(deps)(app):
        assert app.state.module_registry.states["plan-completion-reports"] == "ready"
    assert calls == ["conversation.start", "reports.start", "reports.stop", "conversation.stop"]


def test_delivery_core_and_contracts_do_not_use_domain_orm_rows() -> None:
    core = ast.parse((ROOT / "server" / "app" / "runs" / "delivery.py").read_text())
    forbidden = {"CognitiveGoalRecord", "TaskItemRecord", "DailyBriefRecord", "DailyReviewRecord"}
    assert not {node.id for node in ast.walk(core) if isinstance(node, ast.Name)} & forbidden
    contracts = ast.parse((ROOT / "server" / "app" / "runs" / "delivery_contracts.py").read_text())
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module
        and (node.module.startswith("app.db") or node.module in {"delivery_sources", "budget"})
        for node in ast.walk(contracts)
    )


def test_world_assembly_and_fact_contract_do_not_embed_database_queries() -> None:
    cognition = ROOT / "server" / "app" / "cognition"
    core = ast.parse((cognition / "world.py").read_text())
    forbidden = {
        "select",
        "func",
        "AppUserRecord",
        "ConversationRecord",
        "MessageRecord",
        "DeviceClientRecord",
        "CognitiveDecisionRecord",
        "CognitiveFeedbackRecord",
    }
    assert not {node.id for node in ast.walk(core) if isinstance(node, ast.Name)} & forbidden
    contract = ast.parse((cognition / "world_facts.py").read_text())
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith(("app.db", "sqlalchemy", "app.config", "app.llm"))
        for node in ast.walk(contract)
    )


def test_domain_mapping_exports_keep_one_class_and_table_identity() -> None:
    import app.db as public
    import app.db.models as legacy

    records = json.loads((ROOT / "server" / "architecture" / "mapped_records.json").read_text())
    assert len(records) == len(registered_metadata().tables)
    for name, module in records.items():
        domain = getattr(importlib.import_module(module), name)
        assert domain is getattr(public, name)
        if name != "ModelCostRecord":
            assert domain is getattr(legacy, name)
            assert domain.__module__ == "app.db.models"
        else:
            assert domain.__module__ == "app.db.costs"
        assert domain.__table__ is Base.metadata.tables[domain.__tablename__]
        assert domain.metadata is Base.metadata


def test_proactive_output_core_uses_detached_repository_contracts() -> None:
    output = ROOT / "server" / "app" / "output"
    core = ast.parse((output / "proactive.py").read_text())
    forbidden = {"select", "AppUserRecord", "ProactiveDeliveryReceiptRecord", "AsyncSession"}
    assert not {node.id for node in ast.walk(core) if isinstance(node, ast.Name)} & forbidden
    contract = ast.parse((output / "contracts.py").read_text())
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith(("app.db", "sqlalchemy", "app.config", "app.llm"))
        for node in ast.walk(contract)
    )
    from app.output import ProactiveChannelAttempt as public_attempt
    from app.output import ProactiveDeliveryResult as public_result
    from app.output.contracts import ProactiveChannelAttempt, ProactiveDeliveryResult
    from app.output.proactive import ProactiveChannelAttempt as legacy_attempt
    from app.output.proactive import ProactiveDeliveryResult as legacy_result

    assert public_attempt is legacy_attempt is ProactiveChannelAttempt
    assert public_result is legacy_result is ProactiveDeliveryResult


def test_mapped_ddl_is_unchanged_for_sqlite_and_postgresql() -> None:
    expected = json.loads((ROOT / "server" / "architecture" / "mapped_ddl_sha256.json").read_text())
    actual: dict[str, dict[str, str]] = {}
    for name, table in registered_metadata().tables.items():
        actual[name] = {}
        factories = [
            ("sqlite", cast(Callable[[], Dialect], sqlite.dialect)),
            ("postgresql", cast(Callable[[], Dialect], postgresql.dialect)),
        ]
        for label, factory in factories:
            dialect = factory()
            definition = {
                "table": str(CreateTable(table).compile(dialect=dialect)),
                "indexes": sorted(
                    str(CreateIndex(index).compile(dialect=dialect)) for index in table.indexes
                ),
            }
            actual[name][label] = hashlib.sha256(
                json.dumps(
                    definition, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ).encode()
            ).hexdigest()
    assert actual == expected


def test_domain_mapping_definitions_only_depend_on_shared_base() -> None:
    records = json.loads((ROOT / "server" / "architecture" / "mapped_records.json").read_text())
    for module in set(records.values()):
        path = ROOT / "server" / Path(*module.split(".")).with_suffix(".py")
        source = path.read_text()
        dependencies = set(check_architecture.imports(source, module))
        assert {name for name in dependencies if name.startswith("app.")} <= {
            "app.db.base",
            "app.db.base.Base",
            "app.db.base.BIGINT_PK",
        }
        definitions = {
            node.name for node in ast.parse(source).body if isinstance(node, ast.ClassDef)
        }
        assert definitions == {name for name, owner in records.items() if owner == module}
