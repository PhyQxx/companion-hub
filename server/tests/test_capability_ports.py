"""The provider can run against an execution port without SQL dependencies."""

import ast
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import UUID

from test_capability_runs import Requester
from test_model_capabilities import capability_yaml

from app.config import ConfigStore
from app.harness.operations import GenerationTicket, OperationPolicy
from app.ids import uuid7
from app.llm import EnvSecretProvider
from app.model_capabilities import CapabilityModelService
from app.schemas import PrivacyLevel


class ExecutionPort:
    def __init__(self) -> None:
        self.started = 0
        self.calls: list[tuple[UUID, str, OperationPolicy]] = []

    async def ticket(self, user_id: UUID, task_id: str) -> GenerationTicket | None:
        return None

    async def validate_ticket(self, ticket: GenerationTicket) -> None:
        raise AssertionError("unexpected ticket")

    async def execute(
        self,
        policy: OperationPolicy,
        *,
        user_id: UUID,
        privacy_level: PrivacyLevel,
        entry: str,
        invoke: Callable[[Callable[[], Awaitable[None]]], Awaitable[object]],
        evidence: Callable[[object], dict[str, str]],
        source_guard: Callable[[], Awaitable[None]],
        cost_endpoint: str,
    ) -> object:
        self.calls.append((user_id, entry, policy))

        async def mark_started() -> None:
            self.started += 1

        await source_guard()
        result = await invoke(mark_started)
        await source_guard()
        return result


async def test_provider_delegates_owned_execution_without_database(tmp_path: Path) -> None:
    path = tmp_path / "port.yaml"
    path.write_text(capability_yaml())
    config = ConfigStore(path)
    await config.load()
    execution = ExecutionPort()
    requester = Requester()
    owner = uuid7()
    models = CapabilityModelService(
        config,
        execution=execution,
        request_json=requester,
        secrets=EnvSecretProvider({"ZAI_API_KEY": "test-key"}),
    )
    result = await models.analyze_vision(
        prompt="fixture", image_urls=("https://example.com/image.png",), user_id=owner
    )
    assert result.text == "private synthetic response" and execution.started == 1
    assert execution.calls == [
        (
            owner,
            "capability.vision.analyze",
            OperationPolicy(
                config.current.version,
                tuple(config.current.config.run_budget.model_dump(mode="json").items()),
            ),
        )
    ]
    assert not hasattr(models, "_database")


def test_provider_and_execution_contract_do_not_import_sql_or_record_types() -> None:
    root = Path(__file__).parents[1] / "app"
    provider = ast.parse((root / "model_capabilities.py").read_text())
    assert not any(
        isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sqlalchemy")
        for node in ast.walk(provider)
    )
    assert not (
        {"TaskRunRecord", "ModelCostRecord", "select", "update"}
        & {node.id for node in ast.walk(provider) if isinstance(node, ast.Name)}
    )
    contract = ast.parse((root / "harness" / "operations.py").read_text())
    assert not any(
        isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith(
            ("app.db", "app.config", "app.llm", "app.runs", "sqlalchemy")
        )
        for node in ast.walk(contract)
    )
