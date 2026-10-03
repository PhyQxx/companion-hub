"""Owned media requests reserve declared ceilings; synthetic receipts never prove bills."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select, update
from test_capability_runs import Requester, service
from test_resource_budget import seed
from test_run_cancel_fence import prepared

from app.config import ConfigStore
from app.config.models import RunBudgetConfig
from app.db import ModelCostRecord, TaskRunRecord
from app.harness.budget import BudgetDenied, budget_scope
from app.harness.operations import OperationPolicy
from app.harness.unit_costs import UnitCostQuote, UnitPricing, UnitUsage
from app.llm import ModelEndpoint
from app.model_capabilities import (
    CapabilityModelError,
    CapabilityModelService,
    _endpoint_fingerprint,
)
from app.runs import operation
from app.runs.budget import RunModelBudget
from app.runs.completion import model_owner
from app.runs.store import append_run_event
from app.runs.unit_costs import UnitCostStore
from app.schemas import PrivacyLevel


def configure(config: ConfigStore, *, rate: str = "0.005", cap: float | None = 0.01) -> None:
    value = config.current.config.model_dump(mode="json")
    value["run_budget"].update(cost_currency="CNY", max_daily_cost=cap)
    for name in ("vision", "image", "video"):
        value["models"][name].update(
            cost_currency="CNY",
            request_cost_ceiling=rate,
            query_cost_ceiling="0.001",
            max_retries=3,
        )
    config._current = replace(config.current, config=config.current.config.model_validate(value))


async def invoke(models: CapabilityModelService, owner: UUID, kind: str) -> object:
    if kind == "image":
        return await models.generate_image(prompt="synthetic", user_id=str(owner))
    if kind == "video":
        return await models.generate_video(prompt="synthetic", user_id=str(owner))
    return await models.analyze_vision(
        prompt="synthetic", image_urls=("https://example.com/fixture.png",), user_id=owner
    )


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("kind", ["vision", "image", "video"])
async def test_media_quote_runs_under_currency_caps_without_fabricating_known_usage(
    backend: str,
    kind: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        requester = Requester()
        models, config, owner, _ = await service(storage.database, tmp_path, requester)
        configure(config)
        await invoke(models, owner, kind)
        async with storage.database.sessions() as session:
            run = await session.scalar(select(TaskRunRecord))
            cost = await session.scalar(select(ModelCostRecord))
            assert run is not None and cost is not None
            assert run.user_id == cost.user_id == owner and run.status == "succeeded"
            assert cost.call_id == run.id and cost.unit == "request"
            assert (cost.unit_rate, cost.unit_maximum_quantity) == (Decimal("0.005"), Decimal(1))
            assert (cost.state, cost.charged_micros, cost.reserved_micros) == (
                "unknown",
                5000,
                5000,
            )
            assert cost.unit_quantity is None and cost.input_rate is cost.output_rate is None
            assert cost.settled_at is not None
            assert cost.created_at <= run.updated_at
        assert requester.calls == ["POST"]
        # A second request is admitted exactly at the cap; the third never dispatches.
        await invoke(models, owner, kind)
        with pytest.raises(BudgetDenied, match="daily_cost_budget_exhausted"):
            await invoke(models, owner, kind)
        assert requester.calls == ["POST", "POST"]
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_query_quote_covers_all_retries_even_if_only_two_were_needed(
    backend: str,
    tmp_path: Path,
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
                    raise CapabilityModelError("provider_http_error", detail="HTTP 500: synthetic")
            return super().__call__(method, url, headers, body, timeout)

    storage = await prepared(backend, tmp_path)
    try:
        requester = RetryQuery()
        models, config, owner, _ = await service(storage.database, tmp_path, requester)
        configure(config)
        await models.generate_video(prompt="synthetic", user_id=str(owner))
        await models.async_result("fixture-task", user_id=owner)
        async with storage.database.sessions() as session:
            cost = await session.scalar(
                select(ModelCostRecord).where(ModelCostRecord.reserved_micros == 4000)
            )
            assert cost is not None
            assert cost.unit_maximum_quantity == Decimal(4)
            assert cost.state == "unknown" and cost.charged_micros == 4000
            assert cost.unit_quantity is None
        assert requester.calls == ["POST", "GET", "GET"]
        with pytest.raises(BudgetDenied, match="daily_cost_budget_exhausted"):
            await models.async_result("fixture-task", user_id=owner)
        assert requester.calls == ["POST", "GET", "GET"]
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_query_retry_stops_when_current_cap_tightens_during_first_attempt(
    backend: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        requester = Requester()
        models, config, owner, _ = await service(storage.database, tmp_path, requester)
        configure(config)
        await models.generate_video(prompt="synthetic", user_id=str(owner))
        original = models._request_json

        def change(
            method: str,
            url: str,
            headers: dict[str, str],
            body: dict[str, object] | None,
            timeout: float,
        ) -> object:
            if method == "GET":
                requester.calls.append(method)
                configure(config, cap=0.008)
                raise CapabilityModelError("provider_http_error", detail="HTTP 500: synthetic")
            return original(method, url, headers, body, timeout)

        models._request_json = change
        with pytest.raises(BudgetDenied, match="daily_cost_budget_exhausted"):
            await models.async_result("fixture-task", user_id=owner)
        assert requester.calls == ["POST", "GET"]
        async with storage.database.sessions() as session:
            run = await session.scalar(
                select(TaskRunRecord).where(TaskRunRecord.status == "failed")
            )
            cost = await session.scalar(
                select(ModelCostRecord).where(ModelCostRecord.reserved_micros == 4000)
            )
            assert run is not None and run.contract["operation_state"] == "unknown"
            assert cost is not None and cost.charged_micros == 4000 and cost.state == "unknown"
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("restriction", ["cost", "currency", "cancel", "deadline", "privacy"])
async def test_priced_media_inherits_stricter_parent_authority(
    backend: str,
    restriction: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        requester = Requester()
        models, config, _, _ = await service(storage.database, tmp_path, requester)
        configure(config, cap=None)
        owner, root, _ = await seed(storage.database)
        reasons = {
            "cost": "daily_cost_budget_exhausted",
            "currency": "cost_pricing_unavailable",
            "cancel": "budget_run_inactive",
            "deadline": "run_deadline_exceeded",
            "privacy": "operation_privacy_downgrade",
        }
        changes: dict[str, dict[str, object]] = {
            "cost": {
                "budget": RunBudgetConfig(cost_currency="CNY", max_daily_cost=0.001).model_dump(
                    mode="json"
                )
            },
            "currency": {
                "budget": RunBudgetConfig(cost_currency="USD", max_daily_cost=1).model_dump(
                    mode="json"
                )
            },
            "cancel": {"status": "cancelled"},
            "deadline": {"deadline": datetime.now(UTC) - timedelta(seconds=1)},
            "privacy": {"privacy_level": "L2"},
        }
        async with storage.database.sessions.begin() as session:
            await session.execute(
                update(TaskRunRecord).where(TaskRunRecord.id == root).values(**changes[restriction])
            )
        parent = RunModelBudget(
            storage.database, run_id=root, user_id=owner, config=config.current.config.run_budget
        )
        with (
            budget_scope(parent),
            model_owner(owner),
            pytest.raises(BudgetDenied, match=reasons[restriction]),
        ):
            await models.generate_image(prompt="synthetic")
        assert not requester.calls
        async with storage.database.sessions() as session:
            assert list(await session.scalars(select(ModelCostRecord))) == []
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("stage", ["before_start", "after_start"])
async def test_direct_operation_releases_only_unissued_quote(
    backend: str,
    stage: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, _, _ = await seed(storage.database)
        config = RunBudgetConfig(cost_currency="CNY", max_daily_cost=1)
        quote = UnitCostQuote(
            pricing=UnitPricing(unit="request", currency="CNY", rate_per_unit=Decimal("0.005")),
            maximum_quantity=Decimal(1),
        )
        calls = 0

        async def guard() -> None:
            return None

        async def provider(start: Any) -> str:
            nonlocal calls
            if stage == "before_start":
                raise BudgetDenied("synthetic_source_revoked")
            await start()
            calls += 1
            raise CapabilityModelError("provider_http_error", detail="HTTP 500: synthetic")

        with pytest.raises(
            BudgetDenied if stage == "before_start" else CapabilityModelError,
            match="synthetic_source_revoked" if stage == "before_start" else "provider_http_error",
        ):
            await operation.operate_with_run(
                storage.database,
                OperationPolicy(1, tuple(config.model_dump(mode="json").items()), quote),
                user_id=owner,
                privacy_level=PrivacyLevel.L1,
                entry="synthetic.quote",
                invoke=provider,
                evidence=lambda _: {},
                source_guard=guard,
                cost_endpoint="fixture",
            )
        async with storage.database.sessions() as session:
            cost = await session.scalar(select(ModelCostRecord))
            run = await session.scalar(
                select(TaskRunRecord).where(
                    TaskRunRecord.contract["entry"].as_string() == "synthetic.quote"
                )
            )
            assert cost is not None and run is not None and run.status == "failed"
            assert cost.unit == "request"
            assert (cost.state, cost.charged_micros) == (
                ("estimated", 0) if stage == "before_start" else ("unknown", 5000)
            )
        assert calls == (0 if stage == "before_start" else 1)
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_acceptance_reservation_and_start_recheck_current_cap_inside_transaction(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, _, _ = await seed(storage.database)
        original = RunBudgetConfig(cost_currency="CNY", max_daily_cost=1)
        current = original
        quote = UnitCostQuote(
            pricing=UnitPricing(unit="request", currency="CNY", rate_per_unit=Decimal("0.005")),
            maximum_quantity=Decimal(1),
        )
        changed, calls = False, 0

        async def append_and_tighten(*args: Any, **kwargs: Any) -> None:
            nonlocal current, changed
            await append_run_event(*args, **kwargs)
            if args[2] == "run.accepted":
                current = RunBudgetConfig(cost_currency="CNY", max_daily_cost=0.001)
                changed = True

        monkeypatch.setattr(operation, "append_run_event", append_and_tighten)

        async def guard() -> None:
            return None

        async def provider(start: Any) -> str:
            nonlocal calls
            await start()
            calls += 1
            return "synthetic"

        with pytest.raises(BudgetDenied, match="daily_cost_budget_exhausted"):
            await operation.operate_with_run(
                storage.database,
                OperationPolicy(1, tuple(original.model_dump(mode="json").items()), quote),
                user_id=owner,
                privacy_level=PrivacyLevel.L1,
                entry="synthetic.quote",
                invoke=provider,
                evidence=lambda _: {},
                source_guard=guard,
                cost_endpoint="fixture",
                budget_source=lambda: current,
            )
        assert changed and calls == 0
        async with storage.database.sessions() as session:
            cost = await session.scalar(select(ModelCostRecord))
            assert cost is None  # Tighter cap inside acceptance rolls back the whole transaction.
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_concurrent_media_admission_cannot_overspend_and_late_receipt_survives_delete(
    backend: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        requester = Requester()
        models, config, owner, _ = await service(storage.database, tmp_path, requester)
        configure(config, cap=0.005)
        results = await asyncio.gather(
            *(invoke(models, owner, "image") for _ in range(8)), return_exceptions=True
        )
        assert sum(isinstance(value, BudgetDenied) for value in results) == 7
        assert len(requester.calls) == 1
        async with storage.database.sessions.begin() as session:
            cost = await session.scalar(select(ModelCostRecord))
            assert cost is not None
            call = cost.call_id
            await session.execute(delete(TaskRunRecord).where(TaskRunRecord.id == call))
        await UnitCostStore(storage.database).settle(
            call_id=call,
            user_id=owner,
            usage=UnitUsage(
                quantity=Decimal(0),
                usage_known=True,
                provider_request_id="actual-zero-synthetic-receipt",
            ),
        )
        async with storage.database.sessions() as session:
            row = await session.get_one(ModelCostRecord, call)
            assert row.charged_micros == 0 and row.state == "estimated"
    finally:
        await storage.close()


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1", "1000000001", "0.0000000000001"])
def test_endpoint_ceiling_rejects_bad_precise_price(bad: str) -> None:
    with pytest.raises(ValidationError):
        ModelEndpoint(
            provider="synthetic",
            model="fixture",
            kind="image_generation",
            base_url="https://example.com",
            runs_local=False,
            max_privacy_level="L1",
            cost_currency="CNY",
            request_cost_ceiling=Decimal(bad),
        )


@pytest.mark.parametrize("case", ["missing_currency", "text"])
def test_endpoint_ceiling_requires_declared_media_currency(case: str) -> None:
    with pytest.raises(ValidationError):
        ModelEndpoint(
            provider="synthetic",
            model="fixture",
            kind="text" if case == "text" else "image_generation",
            base_url="https://example.com",
            runs_local=False,
            max_privacy_level="L1",
            cost_currency="CNY" if case == "text" else None,
            request_cost_ceiling=Decimal(0),
        )


def test_price_and_retry_changes_fence_tickets_but_legacy_fingerprint_is_unchanged() -> None:
    import hashlib
    import json

    endpoint = ModelEndpoint(
        provider="synthetic",
        model="fixture",
        kind="video_generation",
        base_url="https://example.com",
        runs_local=False,
        max_privacy_level="L1",
    )
    legacy = {
        "name": "fixture",
        "credentials": {},
        "kind": "video_generation",
        "provider": "synthetic",
        "model": "fixture",
        "base_url": str(endpoint.base_url),
        "runs_local": False,
    }
    assert (
        _endpoint_fingerprint("fixture", endpoint, {})
        == hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
    )
    priced = endpoint.model_copy(
        update={"cost_currency": "CNY", "request_cost_ceiling": Decimal("0.001")}
    )
    assert _endpoint_fingerprint("fixture", endpoint, {}) != _endpoint_fingerprint(
        "fixture", priced, {}
    )
    changed = priced.model_copy(update={"max_retries": 3})
    assert _endpoint_fingerprint("fixture", changed, {}) != _endpoint_fingerprint(
        "fixture", priced, {}
    )
    assert ModelEndpoint.model_validate_json(
        priced.model_dump_json()
    ).request_cost_ceiling == Decimal("0.001")


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_price_revocation_after_dispatch_keeps_original_unknown_reservation(
    backend: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        requester = Requester()
        models, config, owner, _ = await service(storage.database, tmp_path, requester)
        configure(config)
        original = models._request_json

        def changed(
            method: str,
            url: str,
            headers: dict[str, str],
            body: dict[str, object] | None,
            timeout: float,
        ) -> object:
            result = original(method, url, headers, body, timeout)
            configure(config, rate="0.006")
            return result

        models._request_json = changed
        with pytest.raises(BudgetDenied, match="capability_endpoint_changed"):
            await invoke(models, owner, "image")
        async with storage.database.sessions() as session:
            cost = await session.scalar(select(ModelCostRecord))
            run = await session.scalar(select(TaskRunRecord))
            assert cost is not None and run is not None and run.status == "failed"
            assert cost.unit_rate == Decimal("0.005")
            assert cost.state == "unknown" and cost.charged_micros == 5000
        assert requester.calls == ["POST"]
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_cancelled_media_call_retains_unknown_fee_and_never_replays(
    backend: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    requester = Requester()
    requester.block = True
    try:
        models, config, owner, _ = await service(storage.database, tmp_path, requester)
        configure(config)
        task = asyncio.create_task(invoke(models, owner, "image"))
        assert await asyncio.to_thread(requester.entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        requester.release.set()
        async with storage.database.sessions() as session:
            cost = await session.scalar(select(ModelCostRecord))
            run = await session.scalar(select(TaskRunRecord))
            assert cost is not None and run is not None and run.status == "cancelled"
            assert cost.state == "unknown" and cost.charged_micros == 5000
        assert requester.calls == ["POST"]
    finally:
        requester.release.set()
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("mutation", ["release", "price"])
async def test_start_fences_current_ledger_state_and_original_quote(
    backend: str,
    mutation: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, _, _ = await seed(storage.database)
        config = RunBudgetConfig(cost_currency="CNY", max_daily_cost=1)
        quote = UnitCostQuote(
            pricing=UnitPricing(unit="request", currency="CNY", rate_per_unit=Decimal("0.005")),
            maximum_quantity=Decimal(1),
        )
        calls = 0

        async def guard() -> None:
            return None

        async def provider(start: Any) -> str:
            nonlocal calls
            async with storage.database.sessions() as session:
                cost = await session.scalar(select(ModelCostRecord))
                assert cost is not None
                call = cost.call_id
            if mutation == "release":
                assert await UnitCostStore(storage.database).release_unstarted(
                    call_id=call, user_id=owner
                )
            else:
                async with storage.database.sessions.begin() as session:
                    await session.execute(
                        update(ModelCostRecord)
                        .where(ModelCostRecord.call_id == call)
                        .values(unit_rate=Decimal("0.006"))
                    )
            await start()
            calls += 1
            return "synthetic"

        with pytest.raises(BudgetDenied, match="unit_cost_reservation_inactive"):
            await operation.operate_with_run(
                storage.database,
                OperationPolicy(1, tuple(config.model_dump(mode="json").items()), quote),
                user_id=owner,
                privacy_level=PrivacyLevel.L1,
                entry="synthetic.quote",
                invoke=provider,
                evidence=lambda _: {},
                source_guard=guard,
                cost_endpoint="fixture",
            )
        assert calls == 0
        async with storage.database.sessions() as session:
            cost = await session.scalar(select(ModelCostRecord))
            assert cost is not None and cost.state == "estimated" and cost.charged_micros == 0
    finally:
        await storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_relaxed_current_config_cannot_erase_original_media_cap_after_late_fees(
    backend: str,
    tmp_path: Path,
) -> None:
    storage = await prepared(backend, tmp_path)
    try:
        owner, _, _ = await seed(storage.database)
        config = RunBudgetConfig(cost_currency="CNY", max_daily_cost=0.01)
        current = config
        store = UnitCostStore(storage.database)
        from app.ids import uuid7

        prior = uuid7()
        await store.reserve(
            call_id=prior,
            user_id=owner,
            endpoint="prior-synthetic",
            quote=UnitCostQuote(
                pricing=UnitPricing(unit="request", currency="CNY", rate_per_unit=Decimal("0.001")),
                maximum_quantity=Decimal(3),
            ),
            config=config,
        )
        await store.mark_started(call_id=prior, user_id=owner)
        quote = UnitCostQuote(
            pricing=UnitPricing(unit="request", currency="CNY", rate_per_unit=Decimal("0.005")),
            maximum_quantity=Decimal(1),
        )
        calls = 0

        async def guard() -> None:
            return None

        async def provider(start: Any) -> str:
            nonlocal current, calls
            current = RunBudgetConfig()  # A rollback relaxes only new requests.
            await store.settle(
                call_id=prior, user_id=owner, usage=UnitUsage(quantity=Decimal(7), usage_known=True)
            )
            await start()
            calls += 1
            return "synthetic"

        with pytest.raises(BudgetDenied, match="daily_cost_budget_exhausted"):
            await operation.operate_with_run(
                storage.database,
                OperationPolicy(1, tuple(config.model_dump(mode="json").items()), quote),
                user_id=owner,
                privacy_level=PrivacyLevel.L1,
                entry="synthetic.quote",
                invoke=provider,
                evidence=lambda _: {},
                source_guard=guard,
                cost_endpoint="fixture",
                budget_source=lambda: current,
            )
        assert calls == 0
        async with storage.database.sessions() as session:
            costs = list(await session.scalars(select(ModelCostRecord)))
            assert sorted(
                row.charged_micros for row in costs if row.charged_micros is not None
            ) == [0, 7000]
    finally:
        await storage.close()
