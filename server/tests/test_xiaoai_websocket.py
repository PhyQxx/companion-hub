from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import create_xiaoai_websocket_router
from app.auth import AuthService
from app.chat import ChatService
from app.config import DatabaseConfigStore
from app.db import Base, create_database
from app.llm import CompletionRequest, CompletionResult, LLMRoute, ModelUsage

GATEWAY_TOKEN = "xiaoai-gateway-test-token-at-least-20-chars"


def config_yaml() -> str:
    return """
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
    model: local-v1
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


class StreamingBackend:
    async def complete(self, request: CompletionRequest) -> CompletionResult:
        return self._result(request)

    async def stream(
        self,
        request: CompletionRequest,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> CompletionResult:
        await on_delta("今天十点有会议。")
        await on_delta("记得带上方案。")
        return self._result(request)

    @staticmethod
    def _result(request: CompletionRequest) -> CompletionResult:
        return CompletionResult(
            text="今天十点有会议。记得带上方案。",
            provider="openai_compatible",
            model="dialogue-v1",
            endpoint="cloud",
            route=LLMRoute(request.route),
            finish_reason="stop",
            usage=ModelUsage(input_tokens=3, output_tokens=8, total_tokens=11),
            latency_ms=5,
        )


def _build(tmp_path: Path) -> tuple[FastAPI, ChatService]:
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'xiaoai.db'}")
    config_path = tmp_path / "hub.yaml"
    config_path.write_text(config_yaml(), encoding="utf-8")
    store = DatabaseConfigStore(database, config_path)
    auth = AuthService(database)
    service = ChatService(database, store, router_builder=lambda _: StreamingBackend())

    async def setup() -> str:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await store.load()
        session = await auth.setup(
            display_name="Owner", password="correct horse battery staple"
        )
        return str(session.principal.user_id)

    owner_user_id = asyncio.run(setup())
    from uuid import UUID

    app = FastAPI()
    router, _ = create_xiaoai_websocket_router(
        service,
        gateway_token=GATEWAY_TOKEN,
        owner_user_id=UUID(owner_user_id),
    )
    app.include_router(router)
    return app, service


def test_xiaoai_gateway_streams_sentences_and_deduplicates(
    tmp_path: Path,
) -> None:
    app, _ = _build(tmp_path)
    with (
        TestClient(app) as client,
        client.websocket_connect("/ws/adapters/xiaoai") as websocket,
    ):
        websocket.send_json(
            {"type": "authenticate", "gateway_token": GATEWAY_TOKEN}
        )
        assert websocket.receive_json()["type"] == "auth.accepted"
        websocket.send_json(
            {
                "type": "xiaoai.hello",
                "did": "speaker-1",
                "display_name": "客厅",
                "model": "LX06",
            }
        )
        ready = websocket.receive_json()
        assert ready["type"] == "xiaoai.ready"
        assert ready["endpoint_id"] == "xiaoai:speaker-1"
        assert ready["privacy_max"] == "L1"

        query = {
            "type": "xiaoai.query",
            "event_id": "query-100",
            "text": "请阿莉娅告诉我今天的安排",
            "privacy_level": "L1",
        }
        websocket.send_json(query)
        events = _receive_until(websocket, "reply.committed")
        assert [event["type"] for event in events] == [
            "turn.accepted",
            "reply.sentence",
            "reply.sentence",
            "reply.committed",
        ]
        assert [
            event["text"]
            for event in events
            if event["type"] == "reply.sentence"
        ] == ["今天十点有会议。", "记得带上方案。"]
        assert events[-1]["sentence_count"] == 2

        websocket.send_json(query)
        duplicate = websocket.receive_json()
        assert duplicate == {
            "type": "xiaoai.query.duplicate",
            "event_id": "query-100",
        }


def test_xiaoai_gateway_rejects_invalid_auth_and_l2(tmp_path: Path) -> None:
    app, _ = _build(tmp_path)
    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDisconnect) as invalid,
            client.websocket_connect("/ws/adapters/xiaoai") as websocket,
        ):
            websocket.send_json(
                {
                    "type": "authenticate",
                    "gateway_token": "wrong-token-that-is-long-enough",
                }
            )
            websocket.receive_json()
        assert invalid.value.code == 4401

        with client.websocket_connect("/ws/adapters/xiaoai") as websocket:
            websocket.send_json(
                {"type": "authenticate", "gateway_token": GATEWAY_TOKEN}
            )
            websocket.receive_json()
            websocket.send_json(
                {"type": "xiaoai.hello", "did": "speaker-2", "display_name": "卧室"}
            )
            websocket.receive_json()
            websocket.send_json(
                {
                    "type": "xiaoai.query",
                    "event_id": "private-query",
                    "text": "私密内容",
                    "privacy_level": "L2",
                }
            )
            error = websocket.receive_json()
            assert error["type"] == "xiaoai.error"
            assert error["reason_code"] == "invalid_frame"


def _receive_until(websocket: Any, target_type: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while not events or events[-1]["type"] != target_type:
        events.append(websocket.receive_json())
    return events
