"""Owned admin HA inventory with synthetic requests and conservative accounting."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_database_config import config_yaml
from test_run_cancel_fence import prepared

from app.api.admin_config import create_admin_config_router, set_runtime_admin_token
from app.auth import AuthService
from app.config import DatabaseConfigStore, HubConfig
from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, Database, ModelCostRecord, TaskRunRecord
from app.harness.budget import tool_budget_scope
from app.home_assistant import HomeAssistantError, HomeAssistantState
from app.ids import uuid7
from app.runs import operation as operation_module
from app.runs.costs import check_cost_allowance
from app.runs.resources import RunToolBudget, resource_usage


class Client:
    instances: ClassVar[list["Client"]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.key = args[1] if len(args) > 1 else None
        self.calls: list[str] = []
        self.closed = False
        self.instances.append(self)

    async def fetch_states(self) -> tuple[HomeAssistantState, ...]:
        self.calls.append("states")
        now = datetime.now(UTC)
        return (
            HomeAssistantState(
                "light.fixture", "on", {"friendly_name": "Private fixture"}, now, now
            ),
        )

    async def fetch_entity_areas(self) -> dict[str, str]:
        self.calls.append("areas")
        return {"light.fixture": "Private room"}

    async def fetch_entity_devices(self) -> dict[str, dict[str, str | None]]:
        self.calls.append("devices")
        return {"light.fixture": {"device_id": "private-device", "name": "Private fixture"}}

    async def close(self) -> None:
        self.closed = True


@asynccontextmanager
async def fixture(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    owned: bool = True,
    priced: bool = False,
    limit: int = 64,
    cap: float = 0.01,
    ceiling: str = ".002",
    client_type: type[Client] = Client,
) -> AsyncIterator[tuple[AsyncClient, DatabaseConfigStore, Database]]:
    storage = await prepared(backend, tmp_path)
    Client.instances = []
    set_runtime_admin_token(None)
    monkeypatch.setattr("app.api.admin_config.HomeAssistantClient", client_type)
    try:
        if owned:
            await AuthService(storage.database).setup(
                display_name="Synthetic owner", password="synthetic secure test password"
            )
        path = tmp_path / "ha-admin.yaml"
        path.write_text(config_yaml())
        store = DatabaseConfigStore(storage.database, path)
        await store.load()
        data = store.current.config.model_dump(mode="python")
        data["run_budget"] = RunBudgetConfig(
            cost_currency="CNY",
            max_daily_cost=cap,
            max_tool_attempts=limit,
        ).model_dump()
        data["integrations"]["home_assistant"] = {
            "enabled": True,
            "base_url": "https://synthetic.example?secret-query=hidden",
            "secret_value": "synthetic-private-key",
            **(
                {
                    "admin_operation_costs": {
                        operation: {"cost_currency": "CNY", "request_cost_ceiling": ceiling}
                        for operation in ("connection", "inventory")
                    }
                }
                if priced
                else {}
            ),
        }
        draft = await store.create_draft(HubConfig.model_validate(data), actor="synthetic")
        await store.publish(draft.version, actor="synthetic")
        app = FastAPI()
        app.include_router(
            create_admin_config_router(
                store, admin_token="synthetic-admin", database=storage.database
            )
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer synthetic-admin"},
        ) as http:
            yield http, store, storage.database
    finally:
        set_runtime_admin_token(None)
        await storage.close()


async def request(http: AsyncClient, store: DatabaseConfigStore, operation: str) -> Response:
    if operation == "connection":
        return await http.post(
            "/api/v1/admin/config/integrations/home-assistant/test",
            json={
                "config": store.current.config.integrations.home_assistant.model_dump(mode="json")
            },
        )
    return await http.post("/api/v1/admin/config/integrations/home-assistant/entities")


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("operation", ["connection", "inventory"])
async def test_ha_money_cap_without_quote_does_not_create_sdk(
    backend: str,
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch) as (http, store, database):
        response = await request(http, store, operation)
        assert response.status_code == 409
        assert "cost_estimate_unavailable" in response.text
        assert not Client.instances
        async with database.sessions() as sql:
            assert not list(await sql.scalars(select(TaskRunRecord)))
            assert not list(await sql.scalars(select(ModelCostRecord)))


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("operation", ["connection", "inventory"])
async def test_ha_owned_quote_and_each_sdk_attempt(
    backend: str,
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, priced=True) as (http, store, database):
        response = await request(http, store, operation)
        assert response.status_code == 200 and response.json()["ok"]
        client = Client.instances[0]
        assert client.closed
        assert client.calls == (
            ["states"] if operation == "connection" else ["states", "areas", "devices"]
        )
        async with database.sessions() as sql:
            roots = list(await sql.scalars(select(TaskRunRecord)))
            fees = list(await sql.scalars(select(ModelCostRecord)))
        assert len(roots) == len(fees) == 1
        root, fee = roots[0], fees[0]
        assert root.status == "succeeded" and root.user_id == fee.user_id
        assert root.contract["criterion"] == "provider_response_returned"
        assert root.contract["source_actor"] == "admin"
        assert resource_usage(root)["tool_attempts"] == len(client.calls)
        assert resource_usage(root)["returned_tool_calls"] == len(client.calls)
        assert fee.state == "unknown" and fee.charged_micros == 2000
        assert (
            fee.unit == "request" and fee.unit_maximum_quantity == 1 and fee.unit_quantity is None
        )
        projection = json.dumps([root.contract, fee.endpoint])
        for private in (
            "synthetic-private-key",
            "secret-query",
            "Private fixture",
            "Private room",
            "private-device",
        ):
            assert private not in projection


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("operation", ["connection", "inventory"])
async def test_ha_without_setup_has_no_invented_owner(
    backend: str,
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, owned=False, priced=True) as (
        http,
        store,
        database,
    ):
        response = await request(http, store, operation)
        assert response.status_code == 409 and "admin_account_setup_required" in response.text
        assert not Client.instances
        async with database.sessions() as sql:
            assert not list(await sql.scalars(select(TaskRunRecord)))
            assert not list(await sql.scalars(select(ModelCostRecord)))


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_inventory_tool_limit_stops_before_second_http_request(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, priced=True, limit=1) as (
        http,
        store,
        database,
    ):
        response = await request(http, store, "inventory")
        assert response.status_code == 409 and "tool_budget_exhausted" in response.text
        client = Client.instances[0]
        assert client.closed and client.calls == ["states"]
        async with database.sessions() as sql:
            root = (await sql.scalars(select(TaskRunRecord))).one()
            fee = (await sql.scalars(select(ModelCostRecord))).one()
        assert root.status == "failed"
        assert (
            resource_usage(root)["tool_attempts"]
            == resource_usage(root)["returned_tool_calls"]
            == 1
        )
        assert fee.state == "unknown" and fee.charged_micros == 2000


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("method", ["areas", "devices"])
async def test_optional_ha_failure_preserves_transport_evidence_and_counts(
    backend: str,
    method: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Failed(Client):
        async def fetch_entity_areas(self) -> dict[str, str]:
            result = await super().fetch_entity_areas()
            if method == "areas":
                raise HomeAssistantError("synthetic_optional_error")
            return result

        async def fetch_entity_devices(self) -> dict[str, dict[str, str | None]]:
            result = await super().fetch_entity_devices()
            if method == "devices":
                raise HomeAssistantError("synthetic_optional_error")
            return result

    async with fixture(backend, tmp_path, monkeypatch, priced=True, client_type=Failed) as (
        http,
        store,
        database,
    ):
        response = await request(http, store, "inventory")
        assert response.status_code == 200 and response.json()["ok"]
        assert Client.instances[0].closed and Client.instances[0].calls == [
            "states",
            "areas",
            "devices",
        ]
        async with database.sessions() as sql:
            root = (await sql.scalars(select(TaskRunRecord))).one()
            fee = (await sql.scalars(select(ModelCostRecord))).one()
        usage = resource_usage(root)
        assert root.status == "succeeded"
        assert (
            usage["tool_attempts"] == 3
            and usage["returned_tool_calls"] == 2
            and usage["unknown_tool_calls"] == 1
        )
        assert fee.state == "unknown" and fee.charged_micros == 2000


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("operation", ["connection", "inventory"])
async def test_repeated_ha_requests_keep_unknown_fees(
    backend: str,
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, priced=True) as (http, store, database):
        for _ in range(2):
            assert (await request(http, store, operation)).status_code == 200
        async with database.sessions() as sql:
            fees = list(await sql.scalars(select(ModelCostRecord)))
        assert len(fees) == 2 and sum(fee.charged_micros or 0 for fee in fees) == 4000
        assert all(fee.state == "unknown" for fee in fees)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("operation", ["connection", "inventory"])
@pytest.mark.parametrize("ceiling", ["0", ".002"])
async def test_explicit_ha_quote_under_zero_cap(
    backend: str,
    operation: str,
    ceiling: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, priced=True, cap=0, ceiling=ceiling) as (
        http,
        store,
        database,
    ):
        response = await request(http, store, operation)
        assert response.status_code == (200 if ceiling == "0" else 409)
        assert bool(Client.instances) == (ceiling == "0")
        async with database.sessions() as sql:
            fees = list(await sql.scalars(select(ModelCostRecord)))
        assert len(fees) == (1 if ceiling == "0" else 0)
        if fees:
            assert fees[0].state == "unknown" and fees[0].charged_micros == 0


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("operation", ["connection", "inventory"])
@pytest.mark.parametrize("change", ["token", "config", "owner", "cancel", "deadline", "caller"])
async def test_ha_inflight_revocation_joins_slow_cleanup(
    backend: str,
    operation: str,
    change: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, blocker = asyncio.Event(), asyncio.Event()

    class Blocking(Client):
        async def fetch_states(self) -> tuple[HomeAssistantState, ...]:
            rows = await super().fetch_states()
            if operation == "connection":
                entered.set()
                await blocker.wait()
            return rows

        async def fetch_entity_areas(self) -> dict[str, str]:
            rows = await super().fetch_entity_areas()
            entered.set()
            await blocker.wait()
            return rows

        async def close(self) -> None:
            await asyncio.sleep(0.35)
            await super().close()

    async with fixture(backend, tmp_path, monkeypatch, priced=True, client_type=Blocking) as (
        http,
        store,
        database,
    ):
        pending = asyncio.create_task(request(http, store, operation))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if change == "token":
                set_runtime_admin_token("rotated-synthetic-admin")
            elif change == "config":
                data = store.current.config.model_dump(mode="python")
                data["integrations"]["home_assistant"]["secret_value"] = "rotated-private-key"
                draft = await store.create_draft(HubConfig.model_validate(data), actor="synthetic")
                await store.publish(draft.version, actor="synthetic")
            elif change == "caller":
                pending.cancel()
            else:
                async with database.sessions.begin() as sql:
                    root = (await sql.scalars(select(TaskRunRecord))).one()
                    if change == "owner":
                        await sql.execute(update(AppUserRecord).values(status="disabled"))
                    elif change == "cancel":
                        root.contract = {**root.contract, "work_cancel_requested": True}
                    else:
                        root.deadline = datetime.now(UTC) - timedelta(seconds=1)
            if change == "caller":
                with pytest.raises(asyncio.CancelledError):
                    await pending
            else:
                response = await asyncio.wait_for(pending, 3)
                assert response.status_code == (401 if change == "token" else 409)
            client = Client.instances[0]
            assert client.closed
            assert client.calls == (
                ["states"] if operation == "connection" else ["states", "areas"]
            )
            async with database.sessions() as sql:
                root = (await sql.scalars(select(TaskRunRecord))).one()
                fee = (await sql.scalars(select(ModelCostRecord))).one()
            assert root.status == ("cancelled" if change == "caller" else "failed")
            assert resource_usage(root)["unknown_tool_calls"] == 1
            assert fee.state == "unknown" and fee.charged_micros == 2000
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("changed_url", [False, True])
async def test_ha_mask_only_restores_secret_for_saved_address(
    backend: str,
    changed_url: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, priced=True) as (http, store, _):
        config = store.current.config.integrations.home_assistant.model_dump(mode="json")
        config["secret_value"] = "__ARIA_SECRET_CONFIGURED__DO_NOT_EDIT__"
        if changed_url:
            config["base_url"] = "https://other.example"
        response = await http.post(
            "/api/v1/admin/config/integrations/home-assistant/test",
            json={"config": config},
        )
        assert response.status_code == (409 if changed_url else 200)
        if changed_url:
            assert not Client.instances
        else:
            assert Client.instances[0].key == "synthetic-private-key" and Client.instances[0].closed


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", ["rotate", "remove"])
async def test_ha_env_key_revocation_stops_inventory(
    backend: str,
    change: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()

    class Blocking(Client):
        async def fetch_entity_areas(self) -> dict[str, str]:
            self.calls.append("areas")
            entered.set()
            await asyncio.Event().wait()
            return {}

    monkeypatch.setenv("ARIA_SYNTHETIC_HA_KEY", "original-private-key")
    async with fixture(backend, tmp_path, monkeypatch, priced=True, client_type=Blocking) as (
        http,
        store,
        database,
    ):
        data = store.current.config.model_dump(mode="python")
        data["integrations"]["home_assistant"].update(
            secret_value=None, secret_ref="env:ARIA_SYNTHETIC_HA_KEY"
        )
        draft = await store.create_draft(HubConfig.model_validate(data), actor="synthetic")
        await store.publish(draft.version, actor="synthetic")
        pending = asyncio.create_task(request(http, store, "inventory"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if change == "rotate":
                monkeypatch.setenv("ARIA_SYNTHETIC_HA_KEY", "rotated-private-key")
            else:
                monkeypatch.delenv("ARIA_SYNTHETIC_HA_KEY")
            response = await asyncio.wait_for(pending, 2)
            assert response.status_code == 409 and "admin_connection_changed" in response.text
            client = Client.instances[0]
            assert client.closed and client.calls == ["states", "areas"]
            async with database.sessions() as sql:
                fee = (await sql.scalars(select(ModelCostRecord))).one()
            assert fee.state == "unknown" and fee.charged_micros == 2000
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_ha_inventory_retains_original_tool_wallet(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, priced=True) as (http, store, database):
        config = store.current.config.run_budget.model_copy(update={"max_tool_attempts": 1})
        async with database.sessions.begin() as sql:
            owner = (await sql.scalars(select(AppUserRecord.id))).one()
            now = datetime.now(UTC)
            source_id = uuid7()
            sql.add(
                TaskRunRecord(
                    id=source_id,
                    user_id=owner,
                    status="succeeded",
                    privacy_level="L1",
                    budget=config.model_dump(mode="json"),
                    deadline=now + timedelta(minutes=1),
                    created_at=now,
                    updated_at=now,
                    contract={"criterion": "reply_committed"},
                )
            )
        source = RunToolBudget(
            database, run_id=source_id, user_id=owner, config=config, maintenance=True
        )
        with tool_budget_scope(source):
            response = await request(http, store, "inventory")
        assert response.status_code == 409 and "tool_budget_exhausted" in response.text
        assert Client.instances[0].closed and Client.instances[0].calls == ["states"]
        async with database.sessions() as sql:
            parent = await sql.get_one(TaskRunRecord, source_id)
            child = (
                await sql.scalars(
                    select(TaskRunRecord).where(TaskRunRecord.parent_run_id == source_id)
                )
            ).one()
        assert parent.status == "succeeded" and resource_usage(parent)["tool_attempts"] == 1
        assert child.status == "failed" and resource_usage(child).get("tool_attempts", 0) == 0


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("operation", ["connection", "inventory"])
async def test_ha_terminal_fee_wait_rechecks_admin_token(
    backend: str,
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def rotated_check(
        sql: AsyncSession,
        *,
        user_id: UUID,
        amount: int | None,
        currency: str | None,
        config: RunBudgetConfig,
        snapshot: RunBudgetConfig,
        now: datetime,
    ) -> None:
        await check_cost_allowance(
            sql,
            user_id=user_id,
            amount=amount,
            currency=currency,
            config=config,
            snapshot=snapshot,
            now=now,
        )
        returned = await sql.scalar(
            select(TaskRunRecord.id).where(TaskRunRecord.status == "succeeded")
        )
        if returned is not None:
            set_runtime_admin_token("rotated-synthetic-admin")

    monkeypatch.setattr(operation_module, "check_cost_allowance", rotated_check)
    async with fixture(backend, tmp_path, monkeypatch, priced=True) as (http, store, database):
        response = await request(http, store, operation)
        assert response.status_code == 401
        assert Client.instances[0].closed
        async with database.sessions() as sql:
            root = (await sql.scalars(select(TaskRunRecord))).one()
            fee = (await sql.scalars(select(ModelCostRecord))).one()
        assert root.status == "failed" and fee.state == "unknown" and fee.charged_micros == 2000


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_unsaved_ha_connection_can_use_explicit_key_when_saved_env_is_missing(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARIA_SYNTHETIC_MISSING_HA_KEY", raising=False)
    async with fixture(backend, tmp_path, monkeypatch, priced=True) as (http, store, _):
        request_config = store.current.config.integrations.home_assistant.model_dump(mode="json")
        data = store.current.config.model_dump(mode="python")
        data["integrations"]["home_assistant"].update(
            enabled=False,
            secret_value=None,
            secret_ref="env:ARIA_SYNTHETIC_MISSING_HA_KEY",
        )
        draft = await store.create_draft(HubConfig.model_validate(data), actor="synthetic")
        await store.publish(draft.version, actor="synthetic")
        response = await http.post(
            "/api/v1/admin/config/integrations/home-assistant/test",
            json={"config": request_config},
        )
        assert response.status_code == 200 and response.json()["ok"]
        assert Client.instances[0].closed and Client.instances[0].key == "synthetic-private-key"
