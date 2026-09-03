"""Web Push 通知底座：订阅存储、发送器、适配器、配置校验与 API。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import create_push_router
from app.auth import AuthService
from app.config import ConfigStore
from app.config.models import (
    ProactiveChannelConfig,
    ProactiveOutputConfig,
    WebPushConfig,
)
from app.db import AppUserRecord, Base, Database, create_database
from app.ids import uuid7
from app.output.adapter import DeliveryIntent
from app.push import (
    PushSendResult,
    PushSendStatus,
    PushSubscriptionStore,
    VapidCredentials,
    WebPushAdapter,
    WebPushSender,
)
from app.push.sender import resolve_vapid_credentials
from app.schemas.common import PrivacyLevel

PUBLIC_KEY = "BEl62iUYgU34xV6bXHpKvMSa1e4Kr6r7k" + "x" * 40
PRIVATE_KEY = "a" * 43
ENDPOINT = "https://push.example.com/v1/subscription-1"

YAML_TEMPLATE = """
schema_version: 1
models:
  cloud:
    provider: openai_compatible
    model: dialogue-v1
    base_url: https://models.example/v1
    secret_ref: env:MODEL_API_KEY
    runs_local: false
    max_privacy_level: L1
    max_context_tokens: 32768
    input_cost_per_million: 0
    output_cost_per_million: 0
  local:
    provider: openai_compatible
    model: local-model
    base_url: http://127.0.0.1:11434/v1
    runs_local: true
    max_privacy_level: L2
    max_context_tokens: 32768
    input_cost_per_million: 0
    output_cost_per_million: 0
routes:
  dialogue: {primary: cloud}
  utility: {primary: cloud}
  private: {primary: local}
"""


async def _config_store(tmp_path: Path, push_block: str) -> ConfigStore:
    path = tmp_path / "hub.yaml"
    path.write_text(
        YAML_TEMPLATE
        + "integrations:\n  push:\n"
        + push_block
        + "proactive_output:\n  web_push:\n    enabled: true\n    priority: 70\n",
        encoding="utf-8",
    )
    store = ConfigStore(path)
    await store.load()
    return store


def _enabled_push_block() -> str:
    return (
        "    enabled: true\n"
        f"    vapid_public_key: {PUBLIC_KEY}\n"
        f"    vapid_private_key_secret_value: {PRIVATE_KEY}\n"
    )


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    value = create_database("sqlite+aiosqlite:///:memory:")
    async with value.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield value
    finally:
        await value.close()


@pytest.fixture
async def user_id(database: Database) -> UUID:
    value = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=value, display_name="Push owner", status="active"))
    return value


# ---------------------------------------------------------------------------
# 订阅存储
# ---------------------------------------------------------------------------


async def test_subscribe_upserts_by_endpoint(database: Database, user_id: UUID) -> None:
    store = PushSubscriptionStore(database)

    first = await store.subscribe(
        user_id=user_id, endpoint=ENDPOINT, p256dh=PUBLIC_KEY, auth="auth-secret-1"
    )
    other_user = uuid7()
    second = await store.subscribe(
        user_id=other_user, endpoint=ENDPOINT, p256dh=PUBLIC_KEY, auth="auth-secret-2"
    )

    assert second.id == first.id
    assert second.user_id == other_user
    assert second.auth == "auth-secret-2"
    assert len(await store.list_for_user(user_id)) == 0
    assert len(await store.list_for_user(other_user)) == 1


async def test_unsubscribe_is_owner_scoped(database: Database, user_id: UUID) -> None:
    store = PushSubscriptionStore(database)
    await store.subscribe(
        user_id=user_id, endpoint=ENDPOINT, p256dh=PUBLIC_KEY, auth="auth-secret-1"
    )

    stranger = await store.unsubscribe(user_id=uuid4(), endpoint=ENDPOINT)
    owner = await store.unsubscribe(user_id=user_id, endpoint=ENDPOINT)

    assert stranger is False
    assert owner is True
    assert await store.get_by_endpoint(ENDPOINT) is None


async def test_failure_counters(database: Database, user_id: UUID) -> None:
    store = PushSubscriptionStore(database)
    record = await store.subscribe(
        user_id=user_id, endpoint=ENDPOINT, p256dh=PUBLIC_KEY, auth="auth-secret-1"
    )

    await store.mark_failed(record.id)
    await store.mark_failed(record.id)
    middle = await store.get_by_endpoint(ENDPOINT)
    assert middle is not None and middle.consecutive_failures == 2

    await store.mark_delivered(record.id)
    final = await store.get_by_endpoint(ENDPOINT)
    assert final is not None and final.consecutive_failures == 0
    assert final.last_delivered_at is not None


# ---------------------------------------------------------------------------
# 配置校验与凭证解析
# ---------------------------------------------------------------------------


def test_web_push_config_requires_keys_when_enabled() -> None:
    with pytest.raises(ValueError, match="vapid"):
        WebPushConfig(enabled=True)
    with pytest.raises(ValueError, match="vapid"):
        WebPushConfig(enabled=True, vapid_private_key_secret_value=PRIVATE_KEY)
    WebPushConfig(
        enabled=True,
        vapid_public_key=PUBLIC_KEY,
        vapid_private_key_secret_value=PRIVATE_KEY,
    )


def test_proactive_config_forbids_l2_web_push() -> None:
    with pytest.raises(ValueError, match="web push"):
        ProactiveOutputConfig(
            web_push=ProactiveChannelConfig(enabled=True, max_privacy_level="L2")
        )


def test_resolve_vapid_credentials_secret_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_vapid_credentials(WebPushConfig()) is None

    inline = resolve_vapid_credentials(
        WebPushConfig(
            enabled=True,
            vapid_public_key=PUBLIC_KEY,
            vapid_private_key_secret_value=PRIVATE_KEY,
        )
    )
    assert inline == VapidCredentials(PUBLIC_KEY, PRIVATE_KEY, "mailto:admin@example.com")

    monkeypatch.setenv("ARIA_TEST_VAPID_KEY", PRIVATE_KEY)
    via_env = resolve_vapid_credentials(
        WebPushConfig(
            enabled=True,
            vapid_public_key=PUBLIC_KEY,
            vapid_private_key_secret_ref="env:ARIA_TEST_VAPID_KEY",
        )
    )
    assert via_env is not None and via_env.private_key == PRIVATE_KEY

    monkeypatch.delenv("ARIA_TEST_VAPID_KEY")
    assert (
        resolve_vapid_credentials(
            WebPushConfig(
                enabled=True,
                vapid_public_key=PUBLIC_KEY,
                vapid_private_key_secret_ref="env:ARIA_TEST_VAPID_KEY",
            )
        )
        is None
    )


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def test_sender_maps_status_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    from pywebpush import WebPushException  # type: ignore[import-untyped]

    sender = WebPushSender()
    credentials = VapidCredentials(PUBLIC_KEY, PRIVATE_KEY, "mailto:admin@example.com")
    subscription = {
        "endpoint": ENDPOINT,
        "keys": {"p256dh": PUBLIC_KEY, "auth": "auth-secret-1"},
    }
    calls: list[dict[str, Any]] = []

    def fake_webpush(**kwargs: Any) -> _FakeResponse:
        calls.append(kwargs)
        return _FakeResponse(201)

    monkeypatch.setattr("app.push.sender.webpush", fake_webpush)
    ok = sender.send(subscription, b"{}", credentials)
    assert ok.status is PushSendStatus.DELIVERED
    assert calls[0]["vapid_claims"] == {"sub": "mailto:admin@example.com"}
    assert calls[0]["ttl"] == 86_400

    def raising_webpush(**kwargs: Any) -> _FakeResponse:
        del kwargs
        raise WebPushException("gone", response=_FakeResponse(410))

    monkeypatch.setattr("app.push.sender.webpush", raising_webpush)
    gone = sender.send(subscription, b"{}", credentials)
    assert gone.status is PushSendStatus.GONE
    assert gone.status_code == 410


# ---------------------------------------------------------------------------
# 投递适配器
# ---------------------------------------------------------------------------


class FakeSender(WebPushSender):
    def __init__(self, results: list[PushSendResult]) -> None:
        self._results = list(results)
        self.payloads: list[bytes] = []

    def send(
        self, subscription: dict[str, Any], payload: bytes, credentials: VapidCredentials
    ) -> PushSendResult:
        del subscription, credentials
        self.payloads.append(payload)
        return self._results.pop(0)


def _intent(user_id: UUID) -> DeliveryIntent:
    return DeliveryIntent(
        user_id=user_id,
        text="明天 08:00 记得带伞",
        privacy_level=PrivacyLevel.L1,
        trigger_kind="task",
        entity_id="task:1",
        rule_id="reminder",
        decision_id=None,
    )


async def test_adapter_requires_configuration(
    tmp_path: Path, database: Database, user_id: UUID
) -> None:
    config_store = await _config_store(tmp_path, "    enabled: false\n")
    adapter = WebPushAdapter(PushSubscriptionStore(database), config_store=config_store)

    assert adapter.available is False
    receipt = await adapter.deliver(_intent(user_id))
    assert receipt.delivered is False
    assert receipt.reason_code == "push_not_configured"


async def test_adapter_no_subscription(
    tmp_path: Path, database: Database, user_id: UUID
) -> None:
    config_store = await _config_store(tmp_path, _enabled_push_block())
    adapter = WebPushAdapter(PushSubscriptionStore(database), config_store=config_store)

    assert adapter.available is True
    receipt = await adapter.deliver(_intent(user_id))
    assert receipt.delivered is False
    assert receipt.reason_code == "no_push_subscription"


async def test_adapter_delivers_and_prunes_gone(
    tmp_path: Path, database: Database, user_id: UUID
) -> None:
    config_store = await _config_store(tmp_path, _enabled_push_block())
    subscriptions = PushSubscriptionStore(database)
    first = await subscriptions.subscribe(
        user_id=user_id, endpoint=ENDPOINT, p256dh=PUBLIC_KEY, auth="auth-1"
    )
    second = await subscriptions.subscribe(
        user_id=user_id,
        endpoint="https://push.example.com/v1/subscription-2",
        p256dh=PUBLIC_KEY,
        auth="auth-2",
    )
    await subscriptions.mark_failed(first.id)
    sender = FakeSender(
        [
            PushSendResult(PushSendStatus.DELIVERED, 201),
            PushSendResult(PushSendStatus.GONE, 404),
        ]
    )
    adapter = WebPushAdapter(subscriptions, sender, config_store=config_store)

    receipt = await adapter.deliver(_intent(user_id))

    assert receipt.delivered is True
    assert receipt.metadata == {"attempts": 2, "delivered": 1}
    kept = await subscriptions.get_by_endpoint(ENDPOINT)
    assert kept is not None and kept.consecutive_failures == 0
    assert await subscriptions.get_by_endpoint(second.endpoint) is None

    import json

    payload = json.loads(sender.payloads[0])
    assert payload["title"] == "Aria"
    assert payload["tag"] == "aria:task:1"
    assert payload["data"]["trigger_kind"] == "task"


async def test_adapter_all_failed_reports_reason(
    tmp_path: Path, database: Database, user_id: UUID
) -> None:
    config_store = await _config_store(tmp_path, _enabled_push_block())
    subscriptions = PushSubscriptionStore(database)
    await subscriptions.subscribe(
        user_id=user_id, endpoint=ENDPOINT, p256dh=PUBLIC_KEY, auth="auth-1"
    )
    sender = FakeSender([PushSendResult(PushSendStatus.FAILED, 503, "push_service_503")])
    adapter = WebPushAdapter(subscriptions, sender, config_store=config_store)

    receipt = await adapter.deliver(_intent(user_id))

    assert receipt.delivered is False
    assert receipt.reason_code == "push_service_503"
    record = await subscriptions.get_by_endpoint(ENDPOINT)
    assert record is not None and record.consecutive_failures == 1


async def test_adapter_truncates_long_body(
    tmp_path: Path, database: Database, user_id: UUID
) -> None:
    config_store = await _config_store(tmp_path, _enabled_push_block())
    subscriptions = PushSubscriptionStore(database)
    await subscriptions.subscribe(
        user_id=user_id, endpoint=ENDPOINT, p256dh=PUBLIC_KEY, auth="auth-1"
    )
    sender = FakeSender([PushSendResult(PushSendStatus.DELIVERED, 201)])
    adapter = WebPushAdapter(subscriptions, sender, config_store=config_store)
    long_intent = DeliveryIntent(
        user_id=user_id,
        text="长" * 300,
        privacy_level=PrivacyLevel.L1,
        trigger_kind="task",
        entity_id="task:2",
        rule_id="reminder",
        decision_id=None,
    )

    await adapter.deliver(long_intent)

    import json

    payload = json.loads(sender.payloads[0])
    assert len(payload["body"]) == 121
    assert payload["body"].endswith("…")


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


async def test_push_api_lifecycle(tmp_path: Path, database: Database) -> None:
    auth = AuthService(database)
    owner = await auth.setup(display_name="Push user", password="correct horse")
    config_store = await _config_store(tmp_path, _enabled_push_block())
    store = PushSubscriptionStore(database)
    app = FastAPI()
    app.include_router(create_push_router(store, config_store, auth))
    headers = {"Authorization": f"Bearer {owner.access_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.get("/api/v1/push/vapid-key")
        key = await client.get("/api/v1/push/vapid-key", headers=headers)
        bad_scheme = await client.post(
            "/api/v1/push/subscribe",
            headers=headers,
            json={
                "endpoint": "http://insecure.example.com/sub",
                "keys": {"p256dh": PUBLIC_KEY, "auth": "auth-secret-1"},
            },
        )
        created = await client.post(
            "/api/v1/push/subscribe",
            headers=headers,
            json={"endpoint": ENDPOINT, "keys": {"p256dh": PUBLIC_KEY, "auth": "auth-secret-1"}},
        )
        removed = await client.post(
            "/api/v1/push/unsubscribe",
            headers=headers,
            json={"endpoint": ENDPOINT},
        )

    assert unauthorized.status_code == 401
    assert key.status_code == 200
    assert key.json() == {"enabled": True, "public_key": PUBLIC_KEY}
    assert bad_scheme.status_code == 422
    assert created.status_code == 201
    assert removed.status_code == 204
    assert await store.get_by_endpoint(ENDPOINT) is None
