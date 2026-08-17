from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from app.api import create_chat_websocket_router
from app.auth import AuthService
from app.chat import ChatService
from app.config import DatabaseConfigStore
from app.db import Base, InteractionTurnRecord, create_database
from app.llm import CompletionRequest, CompletionResult, LLMRoute, ModelUsage


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


class StreamingBackend:
    async def complete(self, request: CompletionRequest) -> CompletionResult:
        return self._result(request, "hello world")

    async def stream(
        self,
        request: CompletionRequest,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> CompletionResult:
        await on_delta("hello ")
        if request.messages[-1].content == "cancel me":
            await asyncio.sleep(30)
        await on_delta("world")
        return self._result(request, "hello world")

    @staticmethod
    def _result(request: CompletionRequest, text: str) -> CompletionResult:
        return CompletionResult(
            text=text,
            provider="openai_compatible",
            model="dialogue-v1",
            endpoint="cloud",
            route=LLMRoute(request.route),
            finish_reason="stop",
            usage=ModelUsage(input_tokens=3, output_tokens=2, total_tokens=5),
            latency_ms=5,
        )


def test_websocket_stream_cancel_and_cursor_catchup(tmp_path: Path) -> None:
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'ws.db'}")
    config_path = tmp_path / "hub.yaml"
    config_path.write_text(config_yaml(), encoding="utf-8")
    store = DatabaseConfigStore(database, config_path)
    auth = AuthService(database)
    service = ChatService(database, store, router_builder=lambda config: StreamingBackend())

    async def setup() -> tuple[str, str]:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await store.load()
        session = await auth.setup(
            display_name="Owner", password="correct horse battery staple"
        )
        conversation = await service.create_conversation(
            user_id=session.principal.user_id, title="WebSocket"
        )
        return session.access_token, str(conversation.id)

    token, conversation_id = asyncio.run(setup())
    app = FastAPI()
    router, _ = create_chat_websocket_router(service, auth)
    app.include_router(router)

    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDisconnect) as invalid,
            client.websocket_connect("/ws/chat") as websocket,
        ):
            websocket.send_json(
                {"type": "authenticate", "access_token": "invalid-chat-session-token"}
            )
            websocket.receive_json()
        assert invalid.value.code == 4401

        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({"type": "authenticate", "access_token": token})
            assert websocket.receive_json()["type"] == "auth.accepted"
            websocket.send_json(
                {"type": "client_hello", "cursors": {conversation_id: 0}}
            )
            assert websocket.receive_json()["type"] == "sync.completed"
            websocket.send_json(
                {
                    "type": "message.send",
                    "conversation_id": conversation_id,
                    "text": "stream me",
                    "privacy_level": "L1",
                }
            )
            events = _receive_until(websocket, "reply.committed")

        assert [event["type"] for event in events] == [
            "message.committed",
            "turn.accepted",
            "reply.delta",
            "reply.delta",
            "reply.committed",
        ]
        assert [
            event["payload"]["delta"]
            for event in events
            if event["type"] == "reply.delta"
        ] == ["hello ", "world"]
        assert events[-1]["seq"] == 2

        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({"type": "authenticate", "access_token": token})
            websocket.receive_json()
            websocket.send_json(
                {"type": "client_hello", "cursors": {conversation_id: 0}}
            )
            replay = _receive_until(websocket, "sync.completed")
            assert [event["type"] for event in replay] == [
                "message.committed",
                "reply.committed",
                "sync.completed",
            ]

            websocket.send_json(
                {
                    "type": "message.send",
                    "conversation_id": conversation_id,
                    "text": "cancel me",
                    "privacy_level": "L1",
                }
            )
            started = _receive_until(websocket, "reply.delta")
            generation_id = started[-1]["generation_id"]
            websocket.send_json(
                {"type": "turn.cancel", "generation_id": generation_id}
            )
            cancelled = _receive_until(websocket, "turn.cancelled")
            assert cancelled[-1]["payload"]["reason_code"] == "generation_cancelled"

    async def inspect() -> None:
        messages = await service.list_messages_after(
            UUID(conversation_id),
            user_id=(await auth.authenticate(token)).user_id,
            after_seq=0,
        )
        async with database.sessions() as session:
            turns = list(
                await session.scalars(
                    select(InteractionTurnRecord).order_by(InteractionTurnRecord.turn_seq)
                )
            )
        assert [message.role for message in messages] == ["user", "assistant", "user"]
        assert [turn.state for turn in turns] == ["completed", "cancelled"]
        await database.close()

    asyncio.run(inspect())


def _receive_until(websocket: Any, target_type: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while not events or events[-1]["type"] != target_type:
        events.append(websocket.receive_json())
    return events
