"""Synthetic admin Amap probes share one owned workflow and financial ceiling."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from test_admin_ha_harness import fixture as ha_fixture

from app.api.admin_config import set_runtime_admin_token
from app.config import DatabaseConfigStore, HubConfig
from app.db import AppUserRecord, Database, ModelCostRecord, TaskRunRecord
from app.harness.budget import tool_budget_scope
from app.ids import uuid7
from app.runs.resources import RunToolBudget, resource_usage


class Client:
    instances: ClassVar[list["Client"]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.key = args[0] if args else None
        self.calls: list[str] = []
        self.closed = False
        self.instances.append(self)

    async def geocode(self, address: str) -> dict[str, Any]:
        self.calls.append("geocode")
        return {"adcode": "370100"}

    async def weather(self, adcode: str, *, extensions: str) -> dict[str, Any]:
        self.calls.append("weather")
        return {"lives": [{"city": "Synthetic city"}]}

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_amap_unquoted_probe_does_not_create_sdk(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Client.instances = []
    monkeypatch.setattr("app.api.admin_config.AmapProvider", Client)
    async with ha_fixture(backend, tmp_path, monkeypatch) as (http, _, database):
        response = await http.post(
            "/api/v1/admin/config/tools/amap/test",
            json={
                "base_url": "https://synthetic.example/private?key=hidden",
                "secret_value": "private-synthetic-key",
            },
        )
        assert response.status_code == 409
        assert "cost_estimate_unavailable" in response.text
        assert not Client.instances
        async with database.sessions() as sql:
            assert not list(await sql.scalars(select(TaskRunRecord)))
            assert not list(await sql.scalars(select(ModelCostRecord)))


@asynccontextmanager
async def fixture(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    owned: bool = True,
    limit: int = 64,
    cap: float = 0.01,
    client_type: type[Client] = Client,
) -> AsyncIterator[tuple[AsyncClient, DatabaseConfigStore, Database]]:
    Client.instances = []
    monkeypatch.setattr("app.api.admin_config.AmapProvider", client_type)
    async with ha_fixture(
        backend, tmp_path, monkeypatch, owned=owned, limit=limit, cap=cap
    ) as value:
        yield value


def payload(*, ceiling: str = ".002") -> dict[str, object]:
    return {
        "base_url": "https://synthetic.example/private?key=hidden",
        "secret_value": "private-synthetic-key",
        "admin_probe_cost": {"cost_currency": "CNY", "request_cost_ceiling": ceiling},
    }


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_owned_amap_probe_has_two_attempts_and_one_unknown_workflow_ceiling(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch) as (http, _, database):
        response = await http.post("/api/v1/admin/config/tools/amap/test", json=payload())
        assert response.status_code == 200 and response.json()["ok"]
        client = Client.instances[0]
        assert client.closed and client.calls == ["geocode", "weather"]
        async with database.sessions() as sql:
            root = (await sql.scalars(select(TaskRunRecord))).one()
            fee = (await sql.scalars(select(ModelCostRecord))).one()
        assert root.status == "succeeded" and root.user_id == fee.user_id
        assert (
            resource_usage(root)["tool_attempts"]
            == resource_usage(root)["returned_tool_calls"]
            == 2
        )
        assert (
            fee.state == "unknown" and fee.charged_micros == 2000 and fee.unit_maximum_quantity == 1
        )
        public = json.dumps([root.contract, fee.endpoint])
        for private in ("private-synthetic-key", "private?", "key=hidden", "Synthetic city"):
            assert private not in public


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_amap_probe_requires_real_setup_owner(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, owned=False) as (http, _, database):
        response = await http.post("/api/v1/admin/config/tools/amap/test", json=payload())
        assert response.status_code == 409 and "admin_account_setup_required" in response.text
        assert not Client.instances
        async with database.sessions() as sql:
            assert not list(await sql.scalars(select(TaskRunRecord)))
            assert not list(await sql.scalars(select(ModelCostRecord)))


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("mode", ["sdk_error", "schema_invalid"])
async def test_amap_failed_probe_is_not_a_successful_run_and_does_not_expose_sdk_error(
    backend: str,
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Failed(Client):
        async def geocode(self, address: str) -> dict[str, Any]:
            await super().geocode(address)
            if mode == "sdk_error":
                raise RuntimeError(
                    "private-synthetic-key https://synthetic.example/private?key=hidden"
                )
            return {"adcode": None}

    async with fixture(backend, tmp_path, monkeypatch, client_type=Failed) as (http, _, database):
        response = await http.post("/api/v1/admin/config/tools/amap/test", json=payload())
        assert response.status_code == 200 and not response.json()["ok"]
        assert "private-synthetic-key" not in response.text and "key=hidden" not in response.text
        assert Client.instances[0].closed and Client.instances[0].calls == ["geocode", "weather"]
        async with database.sessions() as sql:
            root = (await sql.scalars(select(TaskRunRecord))).one()
            fee = (await sql.scalars(select(ModelCostRecord))).one()
        assert root.status == "failed" and resource_usage(root)["tool_attempts"] == 2
        assert fee.state == "unknown" and fee.charged_micros == 2000


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_amap_first_attempt_limit_prevents_weather_and_does_not_refund(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, limit=1) as (http, _, database):
        response = await http.post("/api/v1/admin/config/tools/amap/test", json=payload())
        assert response.status_code == 409 and "tool_budget_exhausted" in response.text
        assert Client.instances[0].closed and Client.instances[0].calls == ["geocode"]
        async with database.sessions() as sql:
            root = (await sql.scalars(select(TaskRunRecord))).one()
            fee = (await sql.scalars(select(ModelCostRecord))).one()
        assert root.status == "failed" and resource_usage(root)["tool_attempts"] == 1
        assert resource_usage(root)["returned_tool_calls"] == 1
        assert fee.state == "unknown" and fee.charged_micros == 2000


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("ceiling", ["0", ".002"])
async def test_amap_explicit_price_under_zero_cap(
    backend: str,
    ceiling: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch, cap=0) as (http, _, database):
        response = await http.post(
            "/api/v1/admin/config/tools/amap/test", json=payload(ceiling=ceiling)
        )
        assert response.status_code == (200 if ceiling == "0" else 409)
        assert bool(Client.instances) == (ceiling == "0")
        async with database.sessions() as sql:
            fees = list(await sql.scalars(select(ModelCostRecord)))
        assert len(fees) == (1 if ceiling == "0" else 0)
        if fees:
            assert fees[0].state == "unknown" and fees[0].charged_micros == 0


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_amap_unknown_fees_accumulate_across_probes(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with fixture(backend, tmp_path, monkeypatch) as (http, _, database):
        for _ in range(2):
            response = await http.post("/api/v1/admin/config/tools/amap/test", json=payload())
            assert response.status_code == 200 and response.json()["ok"]
        async with database.sessions() as sql:
            fees = list(await sql.scalars(select(ModelCostRecord)))
        assert len(fees) == 2 and sum(fee.charged_micros or 0 for fee in fees) == 4000
        assert all(fee.state == "unknown" for fee in fees)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", ["token", "config", "owner", "cancel", "deadline", "caller"])
async def test_amap_revocation_during_weather_joins_close_and_stops_probe(
    backend: str,
    change: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()

    class Blocking(Client):
        async def weather(self, adcode: str, *, extensions: str) -> dict[str, Any]:
            self.calls.append("weather")
            entered.set()
            await asyncio.Event().wait()
            return {}

        async def close(self) -> None:
            await asyncio.sleep(0.35)
            await super().close()

    async with fixture(backend, tmp_path, monkeypatch, client_type=Blocking) as (
        http,
        store,
        database,
    ):
        pending = asyncio.create_task(
            http.post("/api/v1/admin/config/tools/amap/test", json=payload())
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if change == "token":
                set_runtime_admin_token("rotated-synthetic-admin")
            elif change == "config":
                data = store.current.config.model_dump(mode="python")
                data["tools"]["amap"]["secret_value"] = "rotated-private-key"
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
            assert Client.instances[0].closed and Client.instances[0].calls == [
                "geocode",
                "weather",
            ]
            async with database.sessions() as sql:
                root = (await sql.scalars(select(TaskRunRecord))).one()
                fee = (await sql.scalars(select(ModelCostRecord))).one()
            assert root.status == ("cancelled" if change == "caller" else "failed")
            assert (
                resource_usage(root)["unknown_tool_calls"] == 1
                and resource_usage(root)["returned_tool_calls"] == 1
            )
            assert fee.state == "unknown" and fee.charged_micros == 2000
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("changed_address", [False, True])
async def test_amap_mask_stays_bound_to_saved_address(
    backend: str,
    changed_address: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch) as (http, store, _):
        data = store.current.config.model_dump(mode="python")
        data["tools"]["amap"].update(
            base_url=payload()["base_url"], secret_value="private-synthetic-key"
        )
        draft = await store.create_draft(HubConfig.model_validate(data), actor="synthetic")
        await store.publish(draft.version, actor="synthetic")
        body = payload()
        body["secret_value"] = "__ARIA_SECRET_CONFIGURED__DO_NOT_EDIT__"
        if changed_address:
            body["base_url"] = "https://other.example"
        response = await http.post("/api/v1/admin/config/tools/amap/test", json=body)
        assert response.status_code == (409 if changed_address else 200)
        if changed_address:
            assert not Client.instances
        else:
            assert response.json()["ok"] and Client.instances[0].closed
            assert Client.instances[0].key == "private-synthetic-key"


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", ["rotate", "remove"])
async def test_amap_env_key_revocation_stops_after_geocode(
    backend: str,
    change: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()

    class Blocking(Client):
        async def weather(self, adcode: str, *, extensions: str) -> dict[str, Any]:
            self.calls.append("weather")
            entered.set()
            await asyncio.Event().wait()
            return {}

    monkeypatch.setenv("ARIA_SYNTHETIC_AMAP_KEY", "original-private-key")
    async with fixture(backend, tmp_path, monkeypatch, client_type=Blocking) as (http, _, database):
        body = payload()
        body.pop("secret_value")
        body["secret_ref"] = "env:ARIA_SYNTHETIC_AMAP_KEY"
        pending = asyncio.create_task(http.post("/api/v1/admin/config/tools/amap/test", json=body))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if change == "rotate":
                monkeypatch.setenv("ARIA_SYNTHETIC_AMAP_KEY", "rotated-private-key")
            else:
                monkeypatch.delenv("ARIA_SYNTHETIC_AMAP_KEY")
            response = await asyncio.wait_for(pending, 2)
            assert response.status_code == 409 and "admin_connection_changed" in response.text
            assert Client.instances[0].closed and Client.instances[0].calls == [
                "geocode",
                "weather",
            ]
            async with database.sessions() as sql:
                fee = (await sql.scalars(select(ModelCostRecord))).one()
            assert fee.state == "unknown" and fee.charged_micros == 2000
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("setting", ["disabled", "rollback_looser"])
async def test_config_disable_or_real_rollback_does_not_refill_original_wallet(
    backend: str,
    setting: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch) as (http, store, database):
        config = store.current.config.run_budget.model_copy(update={"max_tool_attempts": 1})
        async with database.sessions.begin() as sql:
            owner = (await sql.scalars(select(AppUserRecord.id))).one()
            now, root_id = datetime.now(UTC), uuid7()
            sql.add(
                TaskRunRecord(
                    id=root_id,
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
        wallet = RunToolBudget(
            database, run_id=root_id, user_id=owner, config=config, maintenance=True
        )
        with tool_budget_scope(wallet):
            response = await http.post("/api/v1/admin/config/tools/amap/test", json=payload())
        assert response.status_code == 409 and "tool_budget_exhausted" in response.text
        data = store.current.config.model_dump(mode="python")
        data["run_budget"] = {"enabled": False}
        draft = await store.create_draft(HubConfig.model_validate(data), actor="synthetic")
        await store.publish(draft.version, actor="synthetic")
        if setting == "rollback_looser":
            rolled = await store.rollback(1, actor="synthetic")
            assert rolled.rollback_from == 1
            assert rolled.config.run_budget.max_tool_attempts > 1
        else:
            assert not store.current.config.run_budget.enabled
        with tool_budget_scope(wallet):
            response = await http.post("/api/v1/admin/config/tools/amap/test", json=payload())
        assert response.status_code == 409 and "tool_budget_exhausted" in response.text
        assert len(Client.instances) == 1 and Client.instances[0].closed
        assert Client.instances[0].calls == ["geocode"]
        async with database.sessions() as sql:
            root = await sql.get_one(TaskRunRecord, root_id)
            fees = list(await sql.scalars(select(ModelCostRecord)))
        assert root.status == "succeeded" and resource_usage(root)["tool_attempts"] == 1
        assert len(fees) == 2 and sum(fee.charged_micros or 0 for fee in fees) == 2000
        assert len([fee for fee in fees if fee.state == "unknown"]) == 1


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_amap_terminal_fee_wait_rechecks_admin_token(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config.models import RunBudgetConfig
    from app.runs import operation as operation_module
    from app.runs.costs import check_cost_allowance

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
    async with fixture(backend, tmp_path, monkeypatch) as (http, _, database):
        response = await http.post("/api/v1/admin/config/tools/amap/test", json=payload())
        assert response.status_code == 401 and Client.instances[0].closed
        async with database.sessions() as sql:
            root = (await sql.scalars(select(TaskRunRecord))).one()
            fee = (await sql.scalars(select(ModelCostRecord))).one()
        assert root.status == "failed" and fee.state == "unknown" and fee.charged_micros == 2000


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("invalid", ["url", "timeout", "concurrency", "rate"])
async def test_invalid_probe_arguments_do_not_admit_a_run_or_sdk(
    backend: str,
    invalid: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture(backend, tmp_path, monkeypatch) as (http, _, database):
        body = payload()
        if invalid == "url":
            body["base_url"] = "file:///private-fixture"
        elif invalid == "timeout":
            body["timeout_ms"] = 0
        elif invalid == "concurrency":
            body["max_concurrency"] = 0
        else:
            body["requests_per_minute"] = 0
        response = await http.post("/api/v1/admin/config/tools/amap/test", json=body)
        assert response.status_code == 422
        assert not Client.instances
        async with database.sessions() as sql:
            assert not list(await sql.scalars(select(TaskRunRecord)))
            assert not list(await sql.scalars(select(ModelCostRecord)))
