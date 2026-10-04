"""Synthetic model self-tests share owned quotas and one workflow fee."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.message import Message
from pathlib import Path
from typing import ClassVar
from urllib.error import HTTPError

import pytest
from sqlalchemy import select, update
from test_admin_ha_harness import fixture

from app.api.admin_config import set_runtime_admin_token
from app.config import HubConfig
from app.db import AppUserRecord, ModelCostRecord, ModelReservationRecord, TaskRunRecord
from app.llm.contracts import (
    CompletionRequest,
    CompletionResult,
    ModelEndpoint,
    ModelUsage,
    ToolCall,
    ToolFunction,
)
from app.runs.resources import resource_usage
from app.schemas import PrivacyLevel


class Provider:
    instances: ClassVar[list["Provider"]] = []

    def __init__(self, name: str, endpoint: ModelEndpoint, secrets: object) -> None:
        self.endpoint = endpoint
        self.calls: list[CompletionRequest] = []
        self.closed = False
        self.instances.append(self)

    async def probe(self) -> None:
        raise AssertionError("unmetered probe must not be used")

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls.append(request)
        return CompletionResult(
            text='{"ok":true}' if self.endpoint.supports_json_mode else "OK",
            provider="synthetic",
            model="fixture",
            endpoint="synthetic",
            route="utility",
            latency_ms=0,
            usage=ModelUsage(
                input_tokens=2,
                output_tokens=1,
                total_tokens=3,
                usage_known=True,
                provider_request_id="synthetic-receipt",
            ),
            tool_calls=[
                ToolCall(id="fixture", function=ToolFunction(name="report_probe", arguments={}))
            ]
            if request.tools
            else [],
        )


def payload(*, priced: bool = True) -> dict[str, object]:
    return {
        "endpoint": {
            "provider": "openai_compatible",
            "model": "fixture",
            "base_url": "https://synthetic.example/private?key=hidden",
            "secret_value": "private-synthetic-key",
            "runs_local": False,
            "max_privacy_level": "L1",
            "max_retries": 3,
            "max_tokens": 8192,
            **(
                {"admin_probe_cost_currency": "CNY", "admin_probe_request_cost_ceiling": ".002"}
                if priced
                else {}
            ),
        }
    }


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_model_unquoted_probe_never_calls_provider(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Provider.instances = []
    monkeypatch.setattr("app.api.admin_config.LiteLLMProvider", Provider)
    async with fixture(backend, tmp_path, monkeypatch) as (http, _, database):
        response = await http.post("/api/v1/admin/config/models/test", json=payload(priced=False))
        assert response.status_code == 409
        assert not Provider.instances
        async with database.sessions() as sql:
            assert not list(await sql.scalars(select(TaskRunRecord)))
            assert not list(await sql.scalars(select(ModelCostRecord)))


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("kind", ["text", "json", "tools", "media", "catalogue", "fallback"])
async def test_model_probe_has_one_fee_and_exact_resource_attempts(
    backend: str, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Provider.instances = []
    monkeypatch.setattr("app.api.admin_config.LiteLLMProvider", Provider)
    body = payload()
    endpoint = body["endpoint"]
    assert isinstance(endpoint, dict)
    endpoint.update(supports_json_mode=kind == "json", supports_tool_calling=kind == "tools")
    if kind == "media":
        endpoint.update(
            kind="image_generation",
            provider="zhipu_native",
            cost_currency="USD",
            request_cost_ceiling="99",
        )
    native_calls: list[str] = []

    def fetch(url: str, **kwargs: object) -> object:
        native_calls.append(url)
        if kind == "fallback":
            raise HTTPError(url, 404, "synthetic missing native API", Message(), None)
        return {
            "models": [
                {"type": "llm", "key": "fixture", "display_name": "fixture", "loaded_instances": []}
            ]
        }

    monkeypatch.setattr("app.api.admin_config._fetch_json", fetch)
    endpoint["runs_local"] = kind in {"catalogue", "fallback"}
    async with fixture(backend, tmp_path, monkeypatch) as (http, _, database):
        response = await http.post("/api/v1/admin/config/models/test", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        async with database.sessions() as sql:
            root = (await sql.scalars(select(TaskRunRecord))).one()
            fee = (await sql.scalars(select(ModelCostRecord))).one()
            reservations = list(await sql.scalars(select(ModelReservationRecord)))
        assert root.status == "succeeded"
        assert root.llm_attempts == (0 if kind == "catalogue" else 1)
        assert root.budget_tokens == (0 if kind == "catalogue" else 3)
        assert resource_usage(root)["tool_attempts"] == (2 if kind == "fallback" else 1)
        assert fee.state == "unknown" and fee.charged_micros == 2000 and fee.currency == "CNY"
        assert "hidden" not in fee.endpoint and "private" not in fee.endpoint
        if reservations:
            assert fee.provider_request_id == "synthetic-receipt"
            assert reservations[0].call_id == fee.call_id
            assert reservations[0].state == "settled" and reservations[0].actual_tokens == 3
            provider = Provider.instances[0]
            assert len(provider.calls) == 1 and provider.endpoint.max_retries == 0
            assert provider.calls[0].max_tokens == (128 if kind == "media" else 1024)
            if kind == "media":
                assert provider.endpoint.model == "glm-4.7-flash"
        assert len(native_calls) == (1 if kind in {"catalogue", "fallback"} else 0)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("failure", ["sdk", "empty", "unknown_usage"])
async def test_model_failure_and_unknown_usage_keep_request_fee(
    backend: str, failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Failed(Provider):
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            result = await super().complete(request)
            if failure == "sdk":
                raise RuntimeError("private-synthetic-key https://private.example?key=hidden")
            return result.model_copy(
                update={"text": ""}
                if failure == "empty"
                else {"usage": ModelUsage(usage_known=False)}
            )

    Provider.instances = []
    monkeypatch.setattr("app.api.admin_config.LiteLLMProvider", Failed)
    async with fixture(backend, tmp_path, monkeypatch) as (http, _, database):
        response = await http.post("/api/v1/admin/config/models/test", json=payload())
        assert response.status_code == 200
        assert response.json()["ok"] is (failure == "unknown_usage")
        assert "private-synthetic-key" not in response.json()["message"]
        async with database.sessions() as sql:
            root = (await sql.scalars(select(TaskRunRecord))).one()
            fee = (await sql.scalars(select(ModelCostRecord))).one()
            reservation = (await sql.scalars(select(ModelReservationRecord))).one()
        assert root.status == ("succeeded" if failure == "unknown_usage" else "failed")
        assert fee.state == "unknown" and fee.charged_micros == 2000
        assert reservation.state == ("settled" if failure == "empty" else "unknown")
        assert len(Provider.instances[0].calls) == 1


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("owned", [False, True])
async def test_model_no_owner_or_zero_money_cap_creates_no_provider(
    backend: str, owned: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Provider.instances = []
    monkeypatch.setattr("app.api.admin_config.LiteLLMProvider", Provider)
    async with fixture(backend, tmp_path, monkeypatch, owned=owned, cap=0) as (http, _, database):
        response = await http.post("/api/v1/admin/config/models/test", json=payload())
        assert response.status_code == 409
        assert not Provider.instances
        async with database.sessions() as sql:
            assert not list(await sql.scalars(select(TaskRunRecord)))


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize(
    "change", ["token", "config", "owner", "cancel", "deadline", "caller", "env", "env_remove"]
)
async def test_model_inflight_rechecks_source_and_joins_provider(
    backend: str, change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()

    class Blocking(Provider):
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            self.calls.append(request)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.35)
                self.closed = True
            raise AssertionError("unreachable")

    Provider.instances = []
    monkeypatch.setattr("app.api.admin_config.LiteLLMProvider", Blocking)
    monkeypatch.setenv("SYNTHETIC_PROBE_KEY", "synthetic-private-key")
    body = payload()
    endpoint = body["endpoint"]
    assert isinstance(endpoint, dict)
    endpoint.update(secret_value=None, secret_ref="env:SYNTHETIC_PROBE_KEY")
    async with fixture(backend, tmp_path, monkeypatch) as (http, store, database):
        pending = asyncio.create_task(http.post("/api/v1/admin/config/models/test", json=body))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if change == "token":
                set_runtime_admin_token("rotated-synthetic-admin")
            elif change == "config":
                data = store.current.config.model_dump(mode="python")
                data["models"]["cloud"]["model"] = "rotated-model"
                draft = await store.create_draft(HubConfig.model_validate(data), actor="synthetic")
                await store.publish(draft.version, actor="synthetic")
            elif change == "caller":
                pending.cancel()
            elif change == "env":
                monkeypatch.setenv("SYNTHETIC_PROBE_KEY", "rotated-private-key")
            elif change == "env_remove":
                monkeypatch.delenv("SYNTHETIC_PROBE_KEY")
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
                assert response.status_code == (401 if change == "token" else 409), response.text
            assert Provider.instances[0].closed and len(Provider.instances[0].calls) == 1
            async with database.sessions() as sql:
                root = (await sql.scalars(select(TaskRunRecord))).one()
                fee = (await sql.scalars(select(ModelCostRecord))).one()
                reservation = (await sql.scalars(select(ModelReservationRecord))).one()
            assert root.status == ("cancelled" if change == "caller" else "failed")
            assert fee.state == "unknown" and fee.charged_micros == 2000
            assert reservation.state == "unknown"
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize(
    "case",
    [
        "token_limit",
        "original_attempts",
        "active_forbidden",
        "fallback_tool_limit",
        "zero_quote",
        "disabled_quota",
    ],
)
async def test_model_probe_respects_original_resource_policy(
    backend: str, case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config.models import RunBudgetConfig
    from app.harness.budget import budget_scope
    from app.ids import uuid7
    from app.runs.budget import RunModelBudget

    Provider.instances = []
    monkeypatch.setattr("app.api.admin_config.LiteLLMProvider", Provider)

    def missing(url: str, **kwargs: object) -> object:
        raise HTTPError(url, 404, "synthetic missing native API", Message(), None)

    monkeypatch.setattr("app.api.admin_config._fetch_json", missing)
    async with fixture(
        backend,
        tmp_path,
        monkeypatch,
        limit=1 if case == "fallback_tool_limit" else 64,
        cap=0 if case == "zero_quote" else 0.01,
    ) as (http, store, database):
        body = payload()
        endpoint = body["endpoint"]
        assert isinstance(endpoint, dict)
        if case == "fallback_tool_limit":
            endpoint["runs_local"] = True
        if case == "zero_quote":
            endpoint["admin_probe_request_cost_ceiling"] = "0"
        data = store.current.config.model_dump(mode="python")
        if case == "token_limit":
            data["run_budget"]["max_tokens"] = 1024
        if case == "disabled_quota":
            data["run_budget"] = RunBudgetConfig(enabled=False).model_dump()
        draft = await store.create_draft(HubConfig.model_validate(data), actor="synthetic")
        await store.publish(draft.version, actor="synthetic")
        parent = None
        parent_id = None
        if case in {"original_attempts", "active_forbidden"}:
            original = RunBudgetConfig(max_llm_attempts=2)
            parent_id = uuid7()
            async with database.sessions.begin() as sql:
                owner = (await sql.scalars(select(AppUserRecord.id))).one()
                sql.add(
                    TaskRunRecord(
                        id=parent_id,
                        user_id=owner,
                        status="succeeded" if case == "original_attempts" else "running",
                        privacy_level="L1",
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                        contract={"entry": "synthetic-original"},
                        budget=original.model_dump(mode="json"),
                        deadline=datetime.now(UTC) + timedelta(seconds=30),
                        llm_attempts=2 if case == "original_attempts" else 0,
                    )
                )
            parent = RunModelBudget(
                database,
                run_id=parent_id,
                user_id=owner,
                config=original,
                phase="maintenance",
                allow_active_parent=False,
            )
        with budget_scope(parent):
            response = await http.post("/api/v1/admin/config/models/test", json=body)
        permitted = case in {"zero_quote", "disabled_quota"}
        assert response.status_code == (200 if permitted else 409), response.text
        assert len(Provider.instances) == (1 if permitted else 0)
        async with database.sessions() as sql:
            fees = list(await sql.scalars(select(ModelCostRecord)))
            roots = list(await sql.scalars(select(TaskRunRecord)))
            reservations = list(await sql.scalars(select(ModelReservationRecord)))
        if parent_id is not None:
            original_row = next(row for row in roots if row.id == parent_id)
            assert original_row.llm_attempts == (2 if case == "original_attempts" else 0)
            assert not reservations
        if case in {"token_limit", "original_attempts"}:
            assert fees[0].state == "estimated" and fees[0].charged_micros == 0
        if case == "fallback_tool_limit":
            assert fees[0].state == "unknown" and fees[0].charged_micros == 2000
            assert roots[0].llm_attempts == 1 and roots[0].budget_tokens == 0
        if case == "zero_quote":
            assert fees[0].state == "unknown" and fees[0].charged_micros == 0
        if case == "disabled_quota":
            assert not reservations and roots[0].llm_attempts == 0
            assert fees[0].state == "unknown" and fees[0].charged_micros == 2000


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("changed_address", [False, True])
async def test_model_mask_bound_to_named_saved_endpoint(
    backend: str, changed_address: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Provider.instances = []
    monkeypatch.setattr("app.api.admin_config.LiteLLMProvider", Provider)
    async with fixture(backend, tmp_path, monkeypatch) as (http, store, _):
        data = store.current.config.model_dump(mode="python")
        data["models"]["cloud"].update(secret_ref=None, secret_value="private-synthetic-key")
        draft = await store.create_draft(HubConfig.model_validate(data), actor="synthetic")
        await store.publish(draft.version, actor="synthetic")
        body = payload()
        endpoint = body["endpoint"]
        assert isinstance(endpoint, dict)
        body["endpoint_name"] = "cloud"
        endpoint.update(
            base_url=str(store.current.config.models["cloud"].base_url),
            secret_value="__ARIA_SECRET_CONFIGURED__DO_NOT_EDIT__",
        )
        if changed_address:
            endpoint["base_url"] = "https://other.example/v1"
        response = await http.post("/api/v1/admin/config/models/test", json=body)
        assert response.status_code == (409 if changed_address else 200), response.text
        assert len(Provider.instances) == (0 if changed_address else 1)
        if not changed_address:
            assert Provider.instances[0].endpoint.secret_value == "private-synthetic-key"


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_native_probe_cancel_waits_for_thread_and_never_falls_back(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    Provider.instances = []
    monkeypatch.setattr("app.api.admin_config.LiteLLMProvider", Provider)

    def blocking(url: str, **kwargs: object) -> object:
        entered.set()
        try:
            assert release.wait(5)
            raise HTTPError(url, 404, "synthetic missing native API", Message(), None)
        finally:
            closed.set()

    monkeypatch.setattr("app.api.admin_config._fetch_json", blocking)
    body = payload()
    endpoint = body["endpoint"]
    assert isinstance(endpoint, dict)
    endpoint["runs_local"] = True
    async with fixture(backend, tmp_path, monkeypatch) as (http, _, database):
        pending = asyncio.create_task(http.post("/api/v1/admin/config/models/test", json=body))
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.01)
            pending.cancel()
            await asyncio.sleep(0.35)
            assert not pending.done() and not closed.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert closed.is_set() and not Provider.instances
            async with database.sessions() as sql:
                root = (await sql.scalars(select(TaskRunRecord))).one()
                fee = (await sql.scalars(select(ModelCostRecord))).one()
            assert root.status == "cancelled" and root.llm_attempts == 0
            assert fee.state == "unknown" and fee.charged_micros == 2000
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_model_terminal_fee_wait_rechecks_admin_token(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        if (
            await sql.scalar(select(TaskRunRecord.id).where(TaskRunRecord.status == "succeeded"))
            is not None
        ):
            set_runtime_admin_token("rotated-synthetic-admin")

    Provider.instances = []
    monkeypatch.setattr("app.api.admin_config.LiteLLMProvider", Provider)
    monkeypatch.setattr(operation_module, "check_cost_allowance", rotated_check)
    async with fixture(backend, tmp_path, monkeypatch) as (http, _, database):
        response = await http.post("/api/v1/admin/config/models/test", json=payload())
        assert response.status_code == 401
        async with database.sessions() as sql:
            root = (await sql.scalars(select(TaskRunRecord))).one()
            fee = (await sql.scalars(select(ModelCostRecord))).one()
        assert root.status == "failed" and fee.state == "unknown" and fee.charged_micros == 2000
        assert fee.provider_request_id == "synthetic-receipt"


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_repeated_model_probes_keep_unknown_fees(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Provider.instances = []
    monkeypatch.setattr("app.api.admin_config.LiteLLMProvider", Provider)
    async with fixture(backend, tmp_path, monkeypatch, cap=0.004) as (http, _, database):
        responses = [
            await http.post("/api/v1/admin/config/models/test", json=payload()) for _ in range(3)
        ]
        assert [response.status_code for response in responses] == [200, 200, 409]
        assert len(Provider.instances) == 2
        async with database.sessions() as sql:
            fees = list(await sql.scalars(select(ModelCostRecord)))
        assert len(fees) == 2 and sum(fee.charged_micros or 0 for fee in fees) == 4000
        assert all(fee.state == "unknown" for fee in fees)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("changed", ["fee", "parent", "quote", "replay", "money"])
async def test_unit_metered_model_permit_cannot_reuse_unrelated_or_changed_fee(
    backend: str, changed: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config.models import RunBudgetConfig
    from app.harness.budget import BudgetDenied, budget_scope
    from app.harness.operations import OperationCost, OperationPolicy, current_operation_cost
    from app.harness.unit_costs import UnitCostQuote, UnitPricing
    from app.ids import uuid7
    from app.runs.budget import RunModelBudget
    from app.runs.operation import operate_with_run

    async with fixture(backend, tmp_path, monkeypatch) as (_, store, database):
        async with database.sessions() as sql:
            owner = (await sql.scalars(select(AppUserRecord.id))).one()
        root_id = uuid7()
        config = RunBudgetConfig()
        async with database.sessions.begin() as sql:
            sql.add(
                TaskRunRecord(
                    id=root_id,
                    user_id=owner,
                    status="succeeded",
                    privacy_level="L1",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                    budget=config.model_dump(mode="json"),
                    contract={"entry": "synthetic-original"},
                )
            )
        parent = RunModelBudget(
            database, run_id=root_id, user_id=owner, config=config, phase="maintenance"
        )

        async def guard() -> None:
            pass

        async def invoke(mark_started: object) -> None:
            cost = current_operation_cost()
            assert cost is not None
            if changed in {"fee", "parent"}:
                async with database.sessions.begin() as sql:
                    if changed == "fee":
                        fee = await sql.get_one(ModelCostRecord, cost.call_id)
                        fee.charged_micros = 0
                    else:
                        child = await sql.get_one(TaskRunRecord, cost.call_id)
                        child.parent_run_id = None
            elif changed == "quote":
                cost = OperationCost(
                    cost.call_id,
                    owner,
                    cost.endpoint,
                    UnitCostQuote(
                        pricing=UnitPricing(
                            unit="request", currency="CNY", rate_per_unit=Decimal(".003")
                        ),
                        maximum_quantity=Decimal(1),
                    ),
                )
            elif changed == "money":
                strict = RunModelBudget(
                    database,
                    run_id=root_id,
                    user_id=owner,
                    config=RunBudgetConfig(cost_currency="CNY", max_daily_cost=0.001),
                    phase="maintenance",
                )
                with pytest.raises(BudgetDenied, match="cost"):
                    await strict.reserve_unit_attempt(cost=cost, tokens=1024, final=True)
                return
            else:
                permit = await parent.reserve_unit_attempt(cost=cost, tokens=1024, final=True)
                await parent.settle_unit_attempt(
                    permit.call_id,
                    ModelUsage(input_tokens=0, output_tokens=0, total_tokens=0, usage_known=True),
                )
            with pytest.raises(BudgetDenied, match="unit_cost_reservation_inactive"):
                await parent.reserve_unit_attempt(cost=cost, tokens=1024, final=True)

        quote = UnitCostQuote(
            pricing=UnitPricing(unit="request", currency="CNY", rate_per_unit=Decimal(".002")),
            maximum_quantity=Decimal(1),
        )
        with budget_scope(parent):
            await operate_with_run(
                database,
                OperationPolicy(store.current.version, tuple(config.model_dump().items()), quote),
                user_id=owner,
                privacy_level=PrivacyLevel.L1,
                entry="synthetic-unit-model",
                invoke=invoke,
                evidence=lambda _: {},
                source_guard=guard,
                cost_endpoint="synthetic-unit-model",
                cooperative=True,
            )
        async with database.sessions() as sql:
            root = await sql.get_one(TaskRunRecord, root_id)
            reservation_rows = list(await sql.scalars(select(ModelReservationRecord)))
        assert root.llm_attempts == (1 if changed == "replay" else 0)
        assert len(reservation_rows) == (1 if changed == "replay" else 0)
