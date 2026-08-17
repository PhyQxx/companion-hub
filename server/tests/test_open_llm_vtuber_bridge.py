from __future__ import annotations

import asyncio
import json
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import select

from app.api import create_auth_router, create_chat_router, create_chat_websocket_router
from app.auth import AuthService
from app.chat import ChatService
from app.config import DatabaseConfigStore
from app.db import Base, InteractionTurnRecord, create_database
from app.llm import CompletionRequest, CompletionResult, LLMRoute, ModelUsage
from aria_olv_bridge import AriaBridgeClient, AriaBridgeConfig, AriaBridgeError

CONVERSATION_ID = "0198f267-b829-79fd-a972-00abc70be001"
GENERATION_ID = "0198f267-b829-79fd-a972-00abc70be002"


class StreamingBackend:
    async def complete(self, request: CompletionRequest) -> CompletionResult:
        return self._result(request, "桥接成功")

    async def stream(self, request: CompletionRequest, on_delta: Any) -> CompletionResult:
        await on_delta("桥接")
        if request.messages[-1].content == "cancel me":
            await asyncio.sleep(30)
        await on_delta("成功")
        return self._result(request, "桥接成功")

    @staticmethod
    def _result(request: CompletionRequest, text: str) -> CompletionResult:
        return CompletionResult(
            text=text,
            provider="openai_compatible",
            model="dialogue-v1",
            endpoint="cloud",
            route=LLMRoute(request.route),
            finish_reason="stop",
            usage=ModelUsage(input_tokens=2, output_tokens=2, total_tokens=4),
            latency_ms=5,
        )


CONFIG_YAML = """
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


def event(event_type: str, **values: object) -> str:
    return json.dumps({"type": event_type, "payload": {}, **values})


class FakeSocket:
    def __init__(self, events: list[str]) -> None:
        self.events: asyncio.Queue[str] = asyncio.Queue()
        for item in events:
            self.events.put_nowait(item)
        self.sent: list[dict[str, Any]] = []
        self.accepted_seen = asyncio.Event()

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        value = await self.events.get()
        if json.loads(value).get("type") == "turn.accepted":
            self.accepted_seen.set()
        return value

    async def close(self) -> None:
        return None


class FakeConnection:
    def __init__(self, socket: FakeSocket) -> None:
        self.socket = socket

    async def __aenter__(self) -> FakeSocket:
        return self.socket

    async def __aexit__(self, *args: object) -> None:
        return None


def http_client(tmp_path: Path) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/auth/login":
            expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
            return httpx.Response(
                200,
                json={"access_token": "session-token-long-enough", "expires_at": expires_at},
            )
        if request.method == "GET" and request.url.path.endswith("/conversations"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path.endswith("/conversations"):
            return httpx.Response(201, json={"id": CONVERSATION_ID, "last_seq": 0})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, base_url="http://aria.test"), requests


@pytest.mark.asyncio
async def test_streams_reply_and_keeps_provider_credentials_outside_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_ARIA_PASSWORD", "local-chat-password")
    http, requests = http_client(tmp_path)
    socket = FakeSocket(
        [
            event("auth.accepted"),
            event("sync.completed"),
            event("message.committed", seq=1),
            event("turn.accepted", generation_id=GENERATION_ID),
            event("reply.delta", payload={"delta": "你"}),
            event("reply.delta", payload={"delta": "好"}),
            event(
                "reply.committed",
                seq=2,
                generation_id=GENERATION_ID,
                payload={"message": {"content": "你好"}},
            ),
        ]
    )
    config = AriaBridgeConfig(
        base_url="http://aria.test",
        password_env="TEST_ARIA_PASSWORD",
        mapping_path=tmp_path / "mapping.json",
    )
    client = AriaBridgeClient(
        config,
        http_client=http,
        websocket_connector=lambda _: FakeConnection(socket),
    )
    client.select_history("default", "history-1")

    result = "".join([delta async for delta in client.stream_message("你好")])

    assert result == "你好"
    assert [frame["type"] for frame in socket.sent] == [
        "authenticate",
        "client_hello",
        "message.send",
    ]
    assert socket.sent[-1]["privacy_level"] == "L1"
    assert json.loads((tmp_path / "mapping.json").read_text()) == {
        "default:history-1": CONVERSATION_ID
    }
    assert all("provider" not in request.url.path for request in requests)
    await http.aclose()


@pytest.mark.asyncio
async def test_task_cancellation_propagates_generation_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_ARIA_PASSWORD", "local-chat-password")
    http, _ = http_client(tmp_path)
    socket = FakeSocket(
        [
            event("auth.accepted"),
            event("sync.completed"),
            event("turn.accepted", generation_id=GENERATION_ID),
        ]
    )
    client = AriaBridgeClient(
        AriaBridgeConfig(
            base_url="http://aria.test",
            password_env="TEST_ARIA_PASSWORD",
            mapping_path=tmp_path / "mapping.json",
        ),
        http_client=http,
        websocket_connector=lambda _: FakeConnection(socket),
    )

    async def consume() -> None:
        async for _ in client.stream_message("请生成一段长回复"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(socket.accepted_seen.wait(), timeout=1)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    cancel_frames = [frame for frame in socket.sent if frame["type"] == "turn.cancel"]
    assert cancel_frames == [{"type": "turn.cancel", "generation_id": GENERATION_ID}]
    await http.aclose()


@pytest.mark.asyncio
async def test_real_http_websocket_turn_persists_and_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'bridge.db'}")
    config_path = tmp_path / "hub.yaml"
    config_path.write_text(CONFIG_YAML, encoding="utf-8")
    store = DatabaseConfigStore(database, config_path)
    auth = AuthService(database)
    service = ChatService(database, store, router_builder=lambda _: StreamingBackend())

    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await store.load()
    await auth.setup(display_name="Owner", password="correct horse battery staple")

    app = FastAPI()
    app.include_router(create_auth_router(auth, admin_token=None))
    app.include_router(create_chat_router(service, auth))
    websocket_router, _ = create_chat_websocket_router(service, auth)
    app.include_router(websocket_router)

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", lifespan="off", access_log=False)
    )
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.01)
    assert server.started

    monkeypatch.setenv("E2E_ARIA_PASSWORD", "correct horse battery staple")
    client = AriaBridgeClient(
        AriaBridgeConfig(
            base_url=f"http://127.0.0.1:{port}",
            password_env="E2E_ARIA_PASSWORD",
            mapping_path=tmp_path / "mapping.json",
        )
    )
    client.select_history("e2e", "main")
    try:
        assert "".join([item async for item in client.stream_message("hello")]) == "桥接成功"

        delta_seen = asyncio.Event()

        async def consume_cancelled_turn() -> None:
            async for _ in client.stream_message("cancel me"):
                delta_seen.set()

        turn_task = asyncio.create_task(consume_cancelled_turn())
        await asyncio.wait_for(delta_seen.wait(), timeout=2)
        await asyncio.sleep(0)
        turn_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn_task
        await asyncio.sleep(0.05)

        mapping = json.loads((tmp_path / "mapping.json").read_text(encoding="utf-8"))
        conversation_id = UUID(mapping["e2e:main"])
        conversations = await service.list_conversations(
            user_id=(await auth.login(password="correct horse battery staple")).principal.user_id
        )
        messages = await service.list_messages_after(
            conversation_id,
            user_id=conversations[0].user_id,
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
    finally:
        await client.close()
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=3)
        await database.close()


@pytest.mark.asyncio
async def test_missing_password_has_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MISSING_ARIA_PASSWORD", raising=False)
    client = AriaBridgeClient(
        AriaBridgeConfig(
            password_env="MISSING_ARIA_PASSWORD",
            mapping_path=tmp_path / "mapping.json",
        )
    )

    with pytest.raises(AriaBridgeError, match="MISSING_ARIA_PASSWORD"):
        await anext(client.stream_message("hello"))

    await client.close()
