from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import JsonValue
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from app.api import create_chat_websocket_router
from app.api.chat_ws import ChatConnection, ChatWebSocketManager
from app.auth import AuthService
from app.chat import ChatService, MessageView
from app.config import DatabaseConfigStore
from app.db import Base, InteractionTurnRecord, create_database
from app.llm import CompletionRequest, CompletionResult, LLMRoute, ModelUsage
from app.runtime import TurnCoordinator
from app.schemas import PrivacyLevel


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
    router, _ = create_chat_websocket_router(
        service,
        auth,
        turn_coordinator=TurnCoordinator(database, service),
    )
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
            initial_sync = websocket.receive_json()
            assert initial_sync["type"] == "sync.completed"
            assert initial_sync["payload"] == {
                "after_seq": 0,
                "next_after_seq": 0,
                "count": 0,
                "has_more": False,
            }
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
                "reply.control",
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


async def test_websocket_cursor_catchup_is_paginated_without_truncation() -> None:
    conversation_id = uuid4()
    user_id = uuid4()
    now = datetime.now(UTC)
    messages = [
        MessageView(
            id=uuid4(),
            conversation_id=conversation_id,
            turn_id=uuid4(),
            seq=seq,
            role="assistant",
            content=f"message-{seq}",
            privacy_level="L1",
            generation_id=None,
            decision_meta=None,
            created_at=now,
        )
        for seq in range(1, 202)
    ]

    class FakeService:
        async def list_messages_after(
            self,
            requested_conversation_id: UUID,
            *,
            user_id: UUID,
            after_seq: int,
            limit: int,
        ) -> list[MessageView]:
            assert requested_conversation_id == conversation_id
            assert user_id == connection.principal.user_id
            return [message for message in messages if message.seq > after_seq][:limit]

    class FakeWebSocket:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        async def send_json(self, event: dict[str, object]) -> None:
            self.events.append(event)

    websocket = FakeWebSocket()
    connection = ChatConnection(
        websocket=websocket,  # type: ignore[arg-type]
        principal=SimpleNamespace(user_id=user_id),  # type: ignore[arg-type]
    )
    manager = ChatWebSocketManager(FakeService())  # type: ignore[arg-type]

    await manager.subscribe(connection, conversation_id, 0)
    first_sync = websocket.events[-1]
    assert first_sync["type"] == "sync.completed"
    assert first_sync["payload"] == {
        "after_seq": 0,
        "next_after_seq": 200,
        "count": 200,
        "has_more": True,
    }
    assert len(websocket.events) == 201

    websocket.events.clear()
    await manager.subscribe(connection, conversation_id, 200)
    second_sync = websocket.events[-1]
    assert second_sync["payload"] == {
        "after_seq": 200,
        "next_after_seq": 201,
        "count": 1,
        "has_more": False,
    }
    assert len(websocket.events) == 2


async def test_device_message_uses_full_chat_service_and_hides_l2_reply(
    tmp_path: Path,
) -> None:
    class AvatarPublisher:
        def __init__(self) -> None:
            self.controls: list[dict[str, JsonValue]] = []

        async def publish_avatar_control(
            self, owner_user_id: UUID, control: dict[str, JsonValue]
        ) -> int:
            del owner_user_id
            self.controls.append(control)
            return 1

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'pet-chat.db'}")
    config_path = tmp_path / "hub.yaml"
    config_path.write_text(config_yaml(), encoding="utf-8")
    store = DatabaseConfigStore(database, config_path)
    auth = AuthService(database)
    service = ChatService(database, store, router_builder=lambda config: StreamingBackend())
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await store.load()
    session = await auth.setup(
        display_name="Owner", password="correct horse battery staple"
    )
    publisher = AvatarPublisher()
    _, manager = create_chat_websocket_router(
        service,
        auth,
        avatar_control_publisher=publisher,
    )

    first, first_speech = await manager.submit_device_message(
        session.principal.user_id, "桌宠普通消息", PrivacyLevel.L1
    )
    second, second_speech = await manager.submit_device_message(
        session.principal.user_id, "桌宠私密消息", PrivacyLevel.L2
    )

    assert first["conversation_id"] == second["conversation_id"]
    assert first_speech == "hello world"
    assert second_speech == "hello world"
    assert len(await service.list_conversations(user_id=session.principal.user_id)) == 1
    messages = await service.list_messages_after(
        UUID(str(first["conversation_id"])),
        user_id=session.principal.user_id,
        after_seq=0,
    )
    assert [message.content for message in messages if message.role == "user"] == [
        "桌宠普通消息",
        "桌宠私密消息",
    ]
    assert publisher.controls[0]["text"] == "hello world"
    assert "text" not in publisher.controls[1]
    await database.close()


def _receive_until(websocket: Any, target_type: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while not events or events[-1]["type"] != target_type:
        events.append(websocket.receive_json())
    return events


def test_send_frame_accepts_and_validates_location_payload() -> None:
    from uuid import uuid4

    from pydantic import ValidationError

    from app.api.chat_ws import SendFrame

    frame = SendFrame.model_validate(
        {
            "type": "message.send",
            "conversation_id": str(uuid4()),
            "text": "今天天气怎么样",
            "privacy_level": "L1",
            "location": {"latitude": 36.66, "longitude": 117.02, "accuracy_m": 25},
        }
    )
    assert frame.location is not None
    assert frame.location.accuracy_m == 25
    assert frame.location.to_client_location().is_fresh()

    with pytest.raises(ValidationError):
        SendFrame.model_validate(
            {
                "type": "message.send",
                "conversation_id": str(uuid4()),
                "text": "hi",
                "location": {"latitude": 95, "longitude": 117.02},
            }
        )


def test_chat_connection_caches_location_across_frames() -> None:
    from unittest.mock import Mock

    from app.api.chat_ws import ChatConnection
    from app.tools import ClientLocationPayload

    connection = ChatConnection(websocket=Mock(), principal=Mock())
    assert connection.resolve_location(None) is None

    first = connection.resolve_location(
        ClientLocationPayload(latitude=36.6, longitude=117.0)
    )
    assert first is not None and first.is_fresh()
    # 后续帧不带 location 时复用连接缓存。
    assert connection.resolve_location(None) is first

    second = connection.resolve_location(
        ClientLocationPayload(latitude=36.7, longitude=117.1)
    )
    assert second is not first
    assert connection.resolve_location(None) is second
