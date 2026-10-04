"""Synthetic admin SDK operations retain authority and conservative costs."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_run_cancel_fence import prepared
from test_senseaudio_admin import FakeSenseAudioClient, bootstrap

from app.api.admin_config import set_runtime_admin_token
from app.api.admin_senseaudio import create_admin_senseaudio_router
from app.auth import AuthService
from app.config import DatabaseConfigStore
from app.config.models import RunBudgetConfig
from app.db import AppUserRecord, Database, ModelCostRecord, TaskRunRecord
from app.integrations.senseaudio import SenseAudioClient, SenseAudioVoice
from app.runs.costs import check_cost_allowance
from app.runs.resources import resource_usage

# Reuse the synthetic config fixture, never the user's runtime configuration.
__all__ = ["bootstrap"]
OPERATIONS = ["voices", "preview", "asr_records", "clone_upload", "clone"]


class Client(FakeSenseAudioClient):
    closed = False

    async def aclose(self) -> None:
        self.closed = True


@asynccontextmanager
async def fixture(
    backend: str,
    tmp_path: Path,
    bootstrap: Path,
    *,
    owned: bool = True,
    priced: bool = True,
    cap: float = 0.01,
    client_type: type[Client] = Client,
) -> AsyncIterator[tuple[AsyncClient, DatabaseConfigStore, Database]]:
    storage = await prepared(backend, tmp_path)
    FakeSenseAudioClient.instances = []
    FakeSenseAudioClient.fail_with = None
    try:
        if owned:
            await AuthService(storage.database).setup(
                display_name="Synthetic owner", password="synthetic secure test password"
            )
        store = DatabaseConfigStore(storage.database, bootstrap)
        await store.load()
        data = store.current.config.model_dump(mode="python")
        data["run_budget"] = RunBudgetConfig(cost_currency="CNY", max_daily_cost=cap).model_dump()
        data["voice"]["senseaudio"] = {
            "enabled": True,
            "secret_value": "synthetic-sdk-key",
            "base_url": "https://synthetic.example/private-path?secret-query=hidden",
            "admin_operation_costs": {
                key: {"cost_currency": "CNY", "request_cost_ceiling": ".002"} for key in OPERATIONS
            }
            if priced
            else {},
        }
        from app.config import HubConfig

        draft = await store.create_draft(HubConfig.model_validate(data), actor="synthetic")
        await store.publish(draft.version, actor="synthetic")
        app = FastAPI()
        app.state.database = storage.database
        app.include_router(
            create_admin_senseaudio_router(
                store,
                database=storage.database,
                admin_token="synthetic-admin",
                client_factory=lambda key, config: cast(
                    SenseAudioClient,
                    client_type(key, base_url=str(config.base_url), tts_model=config.tts_model),
                ),
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


async def request(http: AsyncClient, operation: str) -> Response:
    if operation == "voices":
        return await http.get("/api/v1/admin/senseaudio/voices")
    if operation == "preview":
        return await http.post(
            "/api/v1/admin/senseaudio/preview",
            json={"voice_id": "fixture", "text": "private synthetic preview"},
        )
    if operation == "asr_records":
        return await http.get("/api/v1/admin/senseaudio/asr/records")
    if operation == "clone_upload":
        return await http.post(
            "/api/v1/admin/senseaudio/clone/upload",
            files={"file": ("private-reference.wav", b"synthetic audio", "audio/wav")},
        )
    return await http.post(
        "/api/v1/admin/senseaudio/clone",
        json={
            "file_id": "private-file",
            "label": "private_label",
            "description": "private description",
            "text": "private text",
        },
    )


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_request_owns_one_run_and_one_unknown_fee(
    backend: str, operation: str, tmp_path: Path, bootstrap: Path
) -> None:
    async with fixture(backend, tmp_path, bootstrap) as (http, _store, database):
        response = await request(http, operation)
        assert response.status_code == 200, response.text
        assert len(FakeSenseAudioClient.instances) == 1
        assert isinstance(FakeSenseAudioClient.instances[0], Client)
        assert FakeSenseAudioClient.instances[0].closed
        async with database.sessions() as sql:
            runs = list(await sql.scalars(select(TaskRunRecord)))
            fees = list(await sql.scalars(select(ModelCostRecord)))
        assert len(runs) == len(fees) == 1
        root, fee = runs[0], fees[0]
        assert root.status == "succeeded" and root.parent_run_id is None
        assert root.contract["criterion"] == "provider_response_returned"
        assert root.contract["source_actor"] == "admin"
        assert resource_usage(root)["tool_attempts"] == 1
        assert fee.call_id == root.id and fee.user_id == root.user_id
        assert fee.state == "unknown" and fee.charged_micros == 2000
        assert fee.unit_quantity is None and fee.provider_request_id is None
        persisted = json.dumps([root.contract, fee.endpoint])
        for secret in ("synthetic-sdk-key", "synthetic-admin", "private", "今天天气怎么样"):
            assert secret not in persisted


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_unpriced_call_is_denied_before_client_creation(
    backend: str, operation: str, tmp_path: Path, bootstrap: Path
) -> None:
    async with fixture(backend, tmp_path, bootstrap, priced=False) as (http, _store, database):
        response = await request(http, operation)
        assert response.status_code == 409 and "voice_cost_estimate_unavailable" in response.text
        assert not FakeSenseAudioClient.instances
        async with database.sessions() as sql:
            assert not list(await sql.scalars(select(TaskRunRecord)))
            assert not list(await sql.scalars(select(ModelCostRecord)))


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("reason", ["ownerless", "insufficient", "upstream"])
async def test_owner_cost_and_upstream_failure(
    backend: str, reason: str, tmp_path: Path, bootstrap: Path
) -> None:
    async with fixture(
        backend,
        tmp_path,
        bootstrap,
        owned=reason != "ownerless",
        cap=0.001 if reason == "insufficient" else 0.01,
    ) as (http, _store, database):
        if reason == "upstream":
            FakeSenseAudioClient.fail_with = "synthetic upstream failure"
        response = await request(http, "voices")
        assert response.status_code == (502 if reason == "upstream" else 409)
        if reason != "upstream":
            assert not FakeSenseAudioClient.instances
        else:
            assert isinstance(FakeSenseAudioClient.instances[0], Client)
            assert FakeSenseAudioClient.instances[0].closed
        async with database.sessions() as sql:
            runs = list(await sql.scalars(select(TaskRunRecord)))
            fees = list(await sql.scalars(select(ModelCostRecord)))
        if reason == "upstream":
            assert len(runs) == len(fees) == 1
            assert runs[0].status == "failed" and runs[0].contract["operation_state"] == "unknown"
            assert fees[0].state == "unknown" and fees[0].charged_micros == 2000
        else:
            assert not runs and not fees


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("change", ["token", "config", "owner", "cancel", "deadline", "caller"])
async def test_inflight_revocation_joins_sdk_close(
    backend: str, change: str, tmp_path: Path, bootstrap: Path
) -> None:
    entered, interrupted, closing, closed = (asyncio.Event() for _ in range(4))

    class Blocking(Client):
        async def list_voices(self, voice_type: str = "all") -> list[SenseAudioVoice]:
            entered.set()
            try:
                await asyncio.Event().wait()
                raise AssertionError("blocking provider must be interrupted")
            finally:
                interrupted.set()

        async def aclose(self) -> None:
            closing.set()
            await asyncio.sleep(0.35)
            await super().aclose()
            closed.set()

    async with fixture(backend, tmp_path, bootstrap, client_type=Blocking) as (
        http,
        _store,
        database,
    ):
        pending = asyncio.create_task(request(http, "voices"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            async with database.sessions() as sql:
                root = await sql.scalar(select(TaskRunRecord))
                assert root is not None
            if change == "token":
                set_runtime_admin_token("rotated-admin")
            elif change == "config":
                changed = await http.put(
                    "/api/v1/admin/senseaudio/connection",
                    json={
                        "enabled": True,
                        "base_url": "https://synthetic.example",
                        "secret_value": "rotated-sdk-key",
                    },
                )
                assert changed.status_code == 200
            elif change == "caller":
                pending.cancel()
            else:
                from datetime import UTC, datetime, timedelta

                async with database.sessions.begin() as sql:
                    if change == "owner":
                        await sql.execute(update(AppUserRecord).values(status="deleted"))
                    elif change == "deadline":
                        await sql.execute(
                            update(TaskRunRecord).values(
                                deadline=datetime.now(UTC) - timedelta(seconds=1)
                            )
                        )
                    else:
                        await sql.execute(update(TaskRunRecord).values(status="cancelled"))
            await asyncio.wait_for(closing.wait(), 5)
            assert interrupted.is_set() and not pending.done()
            if change == "caller":
                with pytest.raises(asyncio.CancelledError):
                    await pending
            else:
                response = await asyncio.wait_for(pending, 5)
                assert response.status_code == (401 if change == "token" else 409), response.text
            assert closed.is_set()
            async with database.sessions() as sql:
                root = await sql.get_one(TaskRunRecord, root.id)
                fee = await sql.get_one(ModelCostRecord, root.id)
            assert root.status in {"failed", "cancelled"}
            assert fee.state == "unknown" and fee.charged_micros == 2000
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_connection_preserves_quotes_and_model_override_cannot_reuse_them(
    backend: str, tmp_path: Path, bootstrap: Path
) -> None:
    async with fixture(backend, tmp_path, bootstrap) as (http, store, _database):
        before = store.current.config.voice.senseaudio.admin_operation_costs
        response = await http.put(
            "/api/v1/admin/senseaudio/connection",
            json={
                "enabled": True,
                "base_url": "https://synthetic.example",
                "secret_value": "synthetic-sdk-key",
            },
        )
        assert response.status_code == 200
        assert store.current.config.voice.senseaudio.admin_operation_costs == before
        response = await http.post(
            "/api/v1/admin/senseaudio/preview",
            json={"voice_id": "fixture", "text": "private text", "model": "different-model"},
        )
        assert response.status_code == 409 and not FakeSenseAudioClient.instances
        response = await http.put(
            "/api/v1/admin/senseaudio/connection",
            json={
                "enabled": True,
                "base_url": "https://synthetic.example",
                "secret_value": "synthetic-sdk-key",
                "admin_operation_costs": {},
            },
        )
        assert response.status_code == 200 and response.json()["admin_operation_costs"] == {}


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_repeated_request_cannot_release_unknown_first_fee(
    backend: str, tmp_path: Path, bootstrap: Path
) -> None:
    async with fixture(backend, tmp_path, bootstrap, cap=0.003) as (http, _store, database):
        assert (await request(http, "preview")).status_code == 200
        response = await request(http, "voices")
        assert response.status_code == 409
        assert len(FakeSenseAudioClient.instances) == 1
        async with database.sessions() as sql:
            fees = list(await sql.scalars(select(ModelCostRecord)))
            runs = list(await sql.scalars(select(TaskRunRecord)))
        assert len(fees) == len(runs) == 1
        assert fees[0].state == "unknown" and fees[0].charged_micros == 2000


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_explicit_zero_quote_under_zero_cap(
    backend: str, tmp_path: Path, bootstrap: Path
) -> None:
    async with fixture(backend, tmp_path, bootstrap, cap=0) as (http, _store, database):
        response = await http.put(
            "/api/v1/admin/senseaudio/connection",
            json={
                "enabled": True,
                "base_url": "https://synthetic.example",
                "secret_value": "synthetic-sdk-key",
                "admin_operation_costs": {
                    "voices": {"cost_currency": "CNY", "request_cost_ceiling": "0"}
                },
            },
        )
        assert response.status_code == 200
        assert (await request(http, "voices")).status_code == 200
        async with database.sessions() as sql:
            fee = await sql.scalar(select(ModelCostRecord))
        assert fee is not None and fee.charged_micros == 0 and fee.state == "unknown"


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_oversized_preview_cannot_finish_success(
    backend: str, tmp_path: Path, bootstrap: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.admin_senseaudio.MAX_PREVIEW_BYTES", 10)
    async with fixture(backend, tmp_path, bootstrap) as (http, _store, database):
        assert (await request(http, "preview")).status_code == 413
        async with database.sessions() as sql:
            root = await sql.scalar(select(TaskRunRecord))
            fee = await sql.scalar(select(ModelCostRecord))
        assert root is not None and root.status == "failed"
        assert fee is not None and fee.charged_micros == 2000 and fee.state == "unknown"


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_original_tool_quota_is_not_replaced(
    backend: str, tmp_path: Path, bootstrap: Path
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.harness.budget import budget_scope
    from app.ids import uuid7
    from app.runs.budget import RunModelBudget

    async with fixture(backend, tmp_path, bootstrap) as (http, store, database):
        async with database.sessions() as sql:
            owner = await sql.scalar(select(AppUserRecord.id))
        assert owner is not None
        root_id = uuid7()
        now = datetime.now(UTC)
        config = store.current.config.run_budget.model_copy(update={"max_tool_attempts": 1})
        async with database.sessions.begin() as sql:
            sql.add(
                TaskRunRecord(
                    id=root_id,
                    user_id=owner,
                    request_id=f"synthetic:{root_id}",
                    status="running",
                    privacy_level="L1",
                    created_at=now,
                    updated_at=now,
                    deadline=now + timedelta(seconds=30),
                    budget=config.model_dump(mode="json"),
                    contract={},
                )
            )
        with budget_scope(RunModelBudget(database, run_id=root_id, user_id=owner, config=config)):
            assert (await request(http, "voices")).status_code == 200
            assert (await request(http, "voices")).status_code == 409
        assert len(FakeSenseAudioClient.instances) == 1
        async with database.sessions() as sql:
            root = await sql.get_one(TaskRunRecord, root_id)
            children = list(
                await sql.scalars(
                    select(TaskRunRecord).where(TaskRunRecord.parent_run_id == root_id)
                )
            )
            fees = list(await sql.scalars(select(ModelCostRecord)))
        assert root.status == "running" and resource_usage(root)["tool_attempts"] == 1
        assert sorted(row.status for row in children) == ["failed", "succeeded"]
        assert sum(fee.charged_micros or 0 for fee in fees) == 2000


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_token_rotation_during_terminal_fee_check_rolls_back_success(
    backend: str, tmp_path: Path, bootstrap: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.runs import operation

    original = check_cost_allowance
    rotated = False

    async def allowance(
        sql: AsyncSession,
        *,
        user_id: UUID,
        amount: int | None,
        currency: str | None,
        config: RunBudgetConfig,
        snapshot: RunBudgetConfig,
        now: datetime,
    ) -> None:
        nonlocal rotated
        await original(
            sql,
            user_id=user_id,
            amount=amount,
            currency=currency,
            config=config,
            snapshot=snapshot,
            now=now,
        )
        if await sql.scalar(select(TaskRunRecord.id).where(TaskRunRecord.status == "succeeded")):
            set_runtime_admin_token("rotated-at-terminal-check")
            rotated = True

    async with fixture(backend, tmp_path, bootstrap) as (http, _store, database):
        monkeypatch.setattr(operation, "check_cost_allowance", allowance)
        assert (await request(http, "voices")).status_code == 401
        assert rotated
        async with database.sessions() as sql:
            root = await sql.scalar(select(TaskRunRecord))
            fee = await sql.scalar(select(ModelCostRecord))
        assert root is not None and root.status == "failed"
        assert fee is not None and fee.state == "unknown" and fee.charged_micros == 2000
