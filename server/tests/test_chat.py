from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.api import create_chat_router
from app.chat import ChatService
from app.config import DatabaseConfigStore, HubConfig
from app.db import Base, Database, MessageRecord, create_database
from app.llm import (
    CompletionRequest,
    CompletionResult,
    LLMRoute,
    LLMRouteExhausted,
    ModelUsage,
)
from app.schemas import PrivacyLevel


def config_yaml(model: str = "dialogue-v1") -> str:
    return f"""
schema_version: 1
models:
  cloud:
    provider: openai_compatible
    model: {model}
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
  dialogue: {{primary: cloud}}
  utility: {{primary: cloud}}
  private: {{primary: local}}
"""


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    result = create_database("sqlite+aiosqlite:///:memory:")
    async with result.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield result
    finally:
        await result.close()


@pytest.fixture
async def store(database: Database, tmp_path: Path) -> DatabaseConfigStore:
    path = tmp_path / "hub.yaml"
    path.write_text(config_yaml(), encoding="utf-8")
    result = DatabaseConfigStore(database, path)
    await result.load()
    return result


class FakeRouter:
    def __init__(self, model: str, requests: list[CompletionRequest]) -> None:
        self._model = model
        self._requests = requests

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self._requests.append(request)
        route = LLMRoute.PRIVATE if request.privacy_level == "L2" else request.route
        return CompletionResult(
            text=f"reply from {self._model}",
            provider="openai_compatible",
            model=self._model,
            endpoint="local" if route is LLMRoute.PRIVATE else "cloud",
            route=route,
            finish_reason="stop",
            usage=ModelUsage(input_tokens=10, output_tokens=4, total_tokens=14),
            latency_ms=12.5,
        )


async def test_chat_persists_turn_and_uses_recent_context(
    database: Database, store: DatabaseConfigStore
) -> None:
    requests: list[CompletionRequest] = []
    service = ChatService(
        database,
        store,
        router_builder=lambda config: FakeRouter(config.models["cloud"].model, requests),
    )
    conversation = await service.create_conversation(
        user_id=None, display_name="Test", title="Context test"
    )

    first = await service.send_message(
        conversation.id, text="hello", privacy_level=PrivacyLevel.L1
    )
    second = await service.send_message(
        conversation.id, text="again", privacy_level=PrivacyLevel.L1
    )
    messages = await service.list_messages(conversation.id)

    assert [message.role for message in messages] == ["user", "assistant", "user", "assistant"]
    assert [message.seq for message in messages] == [1, 2, 3, 4]
    assert first.assistant_message.decision_meta == {
        "schema_version": 1,
        "config_version": 1,
        "endpoint": "cloud",
        "provider": "openai_compatible",
        "model": "dialogue-v1",
        "route": "dialogue",
        "finish_reason": "stop",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
            "estimated_cost": 0,
        },
        "latency_ms": 12.5,
    }
    assert [message.content for message in requests[1].messages[1:]] == [
        "hello",
        "reply from dialogue-v1",
        "again",
    ]
    assert second.user_message.turn_id == second.assistant_message.turn_id


async def test_published_database_config_is_used_on_next_turn(
    database: Database, store: DatabaseConfigStore
) -> None:
    seen_models: list[str] = []

    def builder(config: HubConfig) -> FakeRouter:
        seen_models.append(config.models["cloud"].model)
        return FakeRouter(config.models["cloud"].model, [])

    service = ChatService(database, store, router_builder=builder)
    conversation = await service.create_conversation(
        user_id=None, display_name="Test", title=None
    )
    await service.send_message(
        conversation.id, text="before", privacy_level=PrivacyLevel.L1
    )
    candidate = store.current.config.model_dump(mode="python")
    candidate["models"]["cloud"]["model"] = "dialogue-v2"
    draft = await store.create_draft(HubConfig.model_validate(candidate), actor="test")
    await store.publish(draft.version, actor="test")

    result = await service.send_message(
        conversation.id, text="after", privacy_level=PrivacyLevel.L1
    )

    assert seen_models == ["dialogue-v1", "dialogue-v2"]
    assert result.assistant_message.decision_meta is not None
    assert result.assistant_message.decision_meta["config_version"] == 2
    assert result.assistant_message.decision_meta["model"] == "dialogue-v2"


class FailingRouter:
    async def complete(self, request: CompletionRequest) -> CompletionResult:
        del request
        raise LLMRouteExhausted("all_model_routes_failed")


async def test_model_failure_keeps_user_message_for_retry(
    database: Database, store: DatabaseConfigStore
) -> None:
    service = ChatService(database, store, router_builder=lambda config: FailingRouter())
    conversation = await service.create_conversation(
        user_id=None, display_name="Test", title=None
    )

    with pytest.raises(LLMRouteExhausted, match="all_model_routes_failed"):
        await service.send_message(
            conversation.id, text="keep me", privacy_level=PrivacyLevel.L1
        )

    messages = await service.list_messages(conversation.id)
    assert [(message.seq, message.role, message.content) for message in messages] == [
        (1, "user", "keep me")
    ]


async def test_chat_api_auth_validation_and_stable_failure(
    database: Database, store: DatabaseConfigStore
) -> None:
    service = ChatService(database, store, router_builder=lambda config: FailingRouter())
    app = FastAPI()
    app.include_router(create_chat_router(service, admin_token="test-token"))
    headers = {"Authorization": "Bearer test-token"}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        unauthorized = await client.get("/api/v1/chat/conversations")
        created = await client.post(
            "/api/v1/chat/conversations",
            headers=headers,
            json={"display_name": "Test", "title": "API test"},
        )
        conversation_id = created.json()["id"]
        l3 = await client.post(
            f"/api/v1/chat/conversations/{conversation_id}/messages",
            headers=headers,
            json={"text": "must not persist", "privacy_level": "L3"},
        )
        failure = await client.post(
            f"/api/v1/chat/conversations/{conversation_id}/messages",
            headers=headers,
            json={"text": "persist me", "privacy_level": "L1"},
        )
        messages = await client.get(
            f"/api/v1/chat/conversations/{conversation_id}/messages", headers=headers
        )

    assert unauthorized.status_code == 401
    assert created.status_code == 201
    assert l3.status_code == 422
    assert failure.status_code == 503
    assert failure.json() == {"detail": {"reason_code": "all_model_routes_failed"}}
    assert [item["content"] for item in messages.json()] == ["persist me"]
    async with database.sessions() as session:
        count = await session.scalar(select(func.count()).select_from(MessageRecord))
    assert count == 1
