"""Owned media transport, independent of real models, fees or device capture."""

import asyncio
import hashlib
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update
from test_cognitive_save_guard import database as save_database
from test_model_capabilities import capability_yaml
from test_resource_budget import seed

from app.api.model_capabilities import create_model_capability_router
from app.auth import AuthService
from app.config import ConfigStore
from app.db import AppUserRecord, AuthSessionRecord, Database, TaskRunEventRecord, TaskRunRecord
from app.harness.budget import BudgetDenied, budget_scope
from app.ids import uuid7
from app.llm import EnvSecretProvider
from app.model_capabilities import CapabilityModelError, CapabilityModelService
from app.runs.budget import RunModelBudget
from app.runs.completion import model_owner
from app.runs.store import RunStore
from app.schemas import PrivacyLevel

database = save_database


class Requester:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.entered, self.release = threading.Event(), threading.Event()
        self.block = False
        self.error = False

    def __call__(self, method: str, url: str, headers: Any, body: Any, timeout: float) -> object:
        self.calls.append(method)
        self.entered.set()
        if self.block:
            assert self.release.wait(5)
        if self.error:
            raise CapabilityModelError("provider_http_error", detail="HTTP 500: fixture")
        if url.endswith("/videos/generations"):
            return {"id": "fixture-task", "task_status": "PROCESSING"}
        if method == "GET":
            return {
                "task_status": "SUCCESS",
                "video_result": [{"url": "https://example.com/private.mp4"}],
            }
        if url.endswith("/images/generations"):
            return {"data": [{"url": "https://example.com/private.png"}]}
        return {"choices": [{"message": {"content": "private synthetic response"}}]}


async def service(
    database: Database, tmp_path: Path, requester: Requester
) -> tuple[CapabilityModelService, ConfigStore, UUID, UUID]:
    path = tmp_path / "media.yaml"
    path.write_text(capability_yaml())
    config = ConfigStore(path)
    await config.load()
    owners = (uuid7(), uuid7())
    async with database.sessions.begin() as session:
        session.add_all(
            [AppUserRecord(id=owner, display_name="Fixture", status="active") for owner in owners]
        )
    return (
        CapabilityModelService(
            config,
            database=database,
            request_json=requester,
            secrets=EnvSecretProvider({"ZAI_API_KEY": "test-key"}),
        ),
        config,
        *owners,
    )


@pytest.mark.parametrize("kind", ["vision", "image", "video"])
async def test_media_providers_require_active_owned_transport_and_no_private_audit(
    database: Database, tmp_path: Path, kind: str
) -> None:
    requester = Requester()
    models, _, owner, _ = await service(database, tmp_path, requester)
    with pytest.raises(BudgetDenied, match="model_run_owner_missing"):
        await models.generate_image(prompt="private input")
    with model_owner(owner):
        if kind == "vision":
            await models.analyze_vision(
                prompt="private input", image_urls=("data:image/png;base64,cHJpdmF0ZQ==",)
            )
        elif kind == "image":
            await models.generate_image(prompt="private input")
        else:
            await models.generate_video(prompt="private input")
    assert requester.calls == ["POST"]
    async with database.sessions() as session:
        run = await session.scalar(select(TaskRunRecord))
        assert run is not None and run.user_id == owner and run.status == "succeeded"
        assert run.contract["operation_state"] == "returned"
        assert run.contract["resource_usage"]["tool_attempts"] == 1
        events = list(await session.scalars(select(TaskRunEventRecord)))
    encoded = json.dumps(run.contract) + str([item.payload for item in events])
    for private in (
        "private input",
        "private synthetic response",
        "private.png",
        "cHJpdmF0ZQ",
        "test-key",
    ):
        assert private not in encoded


async def test_video_ticket_is_owned_and_checked_by_authenticated_api(
    database: Database, tmp_path: Path
) -> None:
    requester = Requester()
    models, _, owner, other = await service(database, tmp_path, requester)
    await models.generate_video(prompt="fixture", user_id=str(owner))
    app = FastAPI()
    app.include_router(create_model_capability_router(models, AuthService(database)))
    now = datetime.now(UTC)
    async with database.sessions.begin() as session:
        for identifier, token in ((owner, "fixture-owner"), (other, "fixture-other")):
            session.add(
                AuthSessionRecord(
                    id=uuid7(),
                    user_id=identifier,
                    access_hash=hashlib.sha256(token.encode()).hexdigest(),
                    issued_at=now,
                    expires_at=now + timedelta(hours=1),
                    last_seen_at=now,
                )
            )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture") as client:
        denied = await client.get(
            "/api/v1/model-capabilities/tasks/fixture-task",
            headers={"Authorization": "Bearer fixture-other"},
        )
        assert denied.status_code == 404 and requester.calls == ["POST"]
        accepted = await client.get(
            "/api/v1/model-capabilities/tasks/fixture-task",
            headers={"Authorization": "Bearer fixture-owner"},
        )
        assert accepted.status_code == 200 and requester.calls == ["POST", "GET"]
    assert accepted.json()["video_urls"] == ["https://example.com/private.mp4"]


@pytest.mark.parametrize("change", ["endpoint", "credentials", "owner"])
async def test_video_ticket_cannot_change_provider_or_revive_disabled_owner(
    database: Database, tmp_path: Path, change: str
) -> None:
    requester = Requester()
    models, config, owner, _ = await service(database, tmp_path, requester)
    await models.generate_video(prompt="fixture", user_id=str(owner))
    if change == "owner":
        async with database.sessions.begin() as session:
            await session.execute(
                update(AppUserRecord).where(AppUserRecord.id == owner).values(status="disabled")
            )
    else:
        value = config.current.config.model_dump(mode="json")
        value["models"]["video"]["model" if change == "endpoint" else "secret_value"] = "changed"
        config._current = replace(
            config.current, config=config.current.config.model_validate(value)
        )
    with pytest.raises((CapabilityModelError, BudgetDenied)):
        await models.async_result("fixture-task", user_id=owner)
    assert requester.calls == ["POST"]


async def test_media_calls_share_parent_quota_and_preserve_admission_state(
    database: Database, tmp_path: Path
) -> None:
    requester = Requester()
    models, config, _, _ = await service(database, tmp_path, requester)
    owner, parent, _ = await seed(database)
    budget = RunModelBudget(
        database, run_id=parent, user_id=owner, config=config.current.config.run_budget
    )
    with budget_scope(budget), model_owner(owner):
        await models.generate_image(prompt="fixture")
        with pytest.raises(BudgetDenied, match="tool_budget_exhausted"):
            await models.generate_image(prompt="fixture")
    assert requester.calls == ["POST"]
    async with database.sessions() as session:
        parent_row = await session.get(TaskRunRecord, parent)
        children = list(
            await session.scalars(
                select(TaskRunRecord).where(TaskRunRecord.parent_run_id == parent)
            )
        )
    assert parent_row is not None and parent_row.contract["resource_usage"]["tool_attempts"] == 1
    assert sorted(row.contract["operation_state"] for row in children) == [
        "not_started",
        "returned",
    ]


@pytest.mark.parametrize("revocation", ["owner", "config", "run", "ticket"])
async def test_waiting_media_call_rejects_late_thread_result(
    database: Database, tmp_path: Path, revocation: str
) -> None:
    requester = Requester()
    models, config, owner, _ = await service(database, tmp_path, requester)
    if revocation == "ticket":
        await models.generate_video(prompt="fixture", user_id=str(owner))
    requester.block = True
    requester.entered.clear()
    task = asyncio.create_task(
        models.async_result("fixture-task", user_id=owner)
        if revocation == "ticket"
        else models.generate_video(prompt="fixture", user_id=str(owner))
    )
    assert await asyncio.to_thread(requester.entered.wait, 5)
    try:
        if revocation == "config":
            value = config.current.config.model_dump(mode="json")
            value["models"]["video"]["enabled"] = False
            value["capability_models"]["video_generation"] = None
            config._current = replace(
                config.current, config=config.current.config.model_validate(value)
            )
        else:
            async with database.sessions.begin() as session:
                if revocation == "owner":
                    await session.execute(
                        update(AppUserRecord)
                        .where(AppUserRecord.id == owner)
                        .values(status="disabled")
                    )
                elif revocation == "ticket":
                    await session.execute(
                        delete(TaskRunRecord).where(
                            TaskRunRecord.contract["entry"].as_string() == "capability.video.submit"
                        )
                    )
                else:
                    identifier = await session.scalar(
                        select(TaskRunRecord.id).where(TaskRunRecord.user_id == owner)
                    )
            if revocation == "run":
                assert identifier is not None
                assert (await RunStore(database).cancel_work(identifier, user_id=owner))[0]
        with pytest.raises((BudgetDenied, CapabilityModelError)):
            await asyncio.wait_for(task, 3)
    finally:
        requester.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    async with database.sessions() as session:
        rows = list(await session.scalars(select(TaskRunRecord)))
        events = list(await session.scalars(select(TaskRunEventRecord)))
    assert len(rows) == 1 and rows[0].contract["operation_state"] == "unknown"
    assert "provider_task_id" not in rows[0].contract
    assert any(item.kind == "run.tool.unknown" for item in events)


async def test_media_post_unknown_never_automatically_retries(
    database: Database, tmp_path: Path
) -> None:
    requester = Requester()
    requester.error = True
    models, _, owner, _ = await service(database, tmp_path, requester)
    with pytest.raises(CapabilityModelError):
        await models.generate_video(prompt="fixture", user_id=str(owner))
    assert requester.calls == ["POST"]
    async with database.sessions() as session:
        run = await session.scalar(select(TaskRunRecord))
    assert run is not None and run.contract["operation_state"] == "unknown"


async def test_unpriced_media_cannot_bypass_enabled_monetary_cap(
    database: Database, tmp_path: Path
) -> None:
    requester = Requester()
    models, config, owner, _ = await service(database, tmp_path, requester)
    value = config.current.config.model_dump(mode="json")
    value["run_budget"].update(cost_currency="CNY", max_daily_cost=1)
    config._current = replace(config.current, config=config.current.config.model_validate(value))
    with pytest.raises(BudgetDenied, match="media_cost_estimate_unavailable"):
        await models.generate_video(prompt="fixture", user_id=str(owner))
    assert not requester.calls


@pytest.mark.parametrize("restriction", ["privacy", "cost"])
async def test_media_respects_stricter_parent_when_current_config_is_relaxed(
    database: Database, tmp_path: Path, restriction: str
) -> None:
    requester = Requester()
    models, config, _, _ = await service(database, tmp_path, requester)
    owner, parent, _ = await seed(database)
    async with database.sessions.begin() as session:
        row = await session.get_one(TaskRunRecord, parent)
        if restriction == "privacy":
            row.privacy_level = "L2"
        else:
            row.budget = {**(row.budget or {}), "cost_currency": "CNY", "max_daily_cost": 1}
    parent_budget = RunModelBudget(
        database, run_id=parent, user_id=owner, config=config.current.config.run_budget
    )
    with (
        budget_scope(parent_budget),
        model_owner(owner),
        pytest.raises(
            BudgetDenied,
            match="operation_privacy_downgrade"
            if restriction == "privacy"
            else "media_cost_estimate_unavailable",
        ),
    ):
        await models.generate_image(prompt="fixture", privacy_level=PrivacyLevel.L1)
    assert not requester.calls


async def test_read_only_video_lookup_retries_consume_attempts_separately(
    database: Database, tmp_path: Path
) -> None:
    class RetryQuery(Requester):
        attempts = 0

        def __call__(
            self, method: str, url: str, headers: Any, body: Any, timeout: float
        ) -> object:
            if method == "GET":
                self.attempts += 1
                if self.attempts == 1:
                    self.calls.append(method)
                    raise CapabilityModelError(
                        "provider_http_error", detail="HTTP 500: transient read"
                    )
            return super().__call__(method, url, headers, body, timeout)

    requester = RetryQuery()
    models, _, owner, _ = await service(database, tmp_path, requester)
    await models.generate_video(prompt="fixture", user_id=str(owner))
    await models.async_result("fixture-task", user_id=owner)
    assert requester.calls == ["POST", "GET", "GET"]
    async with database.sessions() as session:
        query = await session.scalar(
            select(TaskRunRecord).where(
                TaskRunRecord.contract["entry"].as_string() == "capability.video.query"
            )
        )
        assert query is not None and query.status == "succeeded"
        assert query.contract["resource_usage"]["tool_attempts"] == 2


async def test_interrupted_provider_run_recovers_without_dispatch(
    database: Database, tmp_path: Path
) -> None:
    from app.runs.operation import recover_expired_operations

    requester = Requester()
    _, _, owner, _ = await service(database, tmp_path, requester)
    identifier = uuid7()
    now = datetime.now(UTC)
    async with database.sessions.begin() as session:
        session.add(
            TaskRunRecord(
                id=identifier,
                user_id=owner,
                status="running",
                privacy_level="L1",
                contract={"criterion": "provider_response_returned", "operation_state": "started"},
                created_at=now - timedelta(minutes=10),
                updated_at=now - timedelta(minutes=10),
                deadline=now - timedelta(seconds=1),
            )
        )
    assert await recover_expired_operations(database) == 1
    assert await recover_expired_operations(database) == 0
    async with database.sessions() as session:
        row = await session.get_one(TaskRunRecord, identifier)
    assert row.status == "failed" and row.contract["operation_state"] == "unknown"
    assert not requester.calls


async def test_unknown_media_fees_survive_source_deletion_and_block_later_cost_caps(
    database: Database, tmp_path: Path
) -> None:
    from app.config.models import RunBudgetConfig
    from app.db import ModelCostRecord
    from app.llm.contracts import ModelPricing
    from app.runs.costs import reserve_cost

    requester = Requester()
    models, _, owner, _ = await service(database, tmp_path, requester)
    await models.generate_video(prompt="fixture", user_id=str(owner))
    async with database.sessions.begin() as session:
        await session.execute(delete(TaskRunRecord).where(TaskRunRecord.user_id == owner))
    async with database.sessions() as session:
        cost = await session.scalar(select(ModelCostRecord).where(ModelCostRecord.user_id == owner))
    assert cost is not None and cost.state == "unknown" and cost.charged_micros is None
    assert cost.input_rate is None and cost.output_rate is None
    assert cost.provider_request_id == "fixture-task"
    policy = RunBudgetConfig(cost_currency="CNY", max_daily_cost=1)
    with pytest.raises(BudgetDenied, match="cost_usage_unknown"):
        async with database.sessions.begin() as session:
            await reserve_cost(
                session,
                call_id=uuid7(),
                user_id=owner,
                endpoint="fixture-text",
                tokens=1,
                pricing=ModelPricing(input_rate=1, output_rate=1, currency="CNY"),
                config=policy,
                snapshot=policy,
                now=datetime.now(UTC),
            )
