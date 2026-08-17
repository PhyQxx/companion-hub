from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect


class AriaBridgeError(RuntimeError):
    """Raised when Aria cannot complete a bridged chat turn."""


class WebSocketLike(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


WebSocketConnector = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class AriaBridgeConfig:
    base_url: str = "http://127.0.0.1:8000"
    password_env: str = "ARIA_CHAT_PASSWORD"
    privacy_level: str = "L1"
    conversation_title: str = "Open-LLM-VTuber"
    mapping_path: Path = Path(".aria/open_llm_vtuber_conversations.json")
    request_timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        if self.privacy_level not in {"L0", "L1", "L2"}:
            raise ValueError("privacy_level must be L0, L1, or L2")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must use http:// or https://")

    @property
    def websocket_url(self) -> str:
        parsed = urlsplit(self.base_url.rstrip("/"))
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunsplit((scheme, parsed.netloc, "/ws/chat", "", ""))


@dataclass(slots=True)
class _Conversation:
    id: str
    last_seq: int


class AriaBridgeClient:
    """Small REST/WebSocket client that keeps model credentials inside Aria."""

    def __init__(
        self,
        config: AriaBridgeConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        websocket_connector: WebSocketConnector = connect,
    ) -> None:
        self.config = config
        self._http = http_client or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            timeout=config.request_timeout_seconds,
        )
        self._owns_http = http_client is None
        self._websocket_connector = websocket_connector
        self._access_token: str | None = None
        self._token_expires_at: datetime | None = None
        self._history_key = "default"
        self._active_socket: WebSocketLike | None = None
        self._active_generation_id: str | None = None
        self._active_loop: asyncio.AbstractEventLoop | None = None
        self.last_agent_reply: dict[str, Any] | None = None

    def select_history(self, conf_uid: str, history_uid: str) -> None:
        self._history_key = f"{conf_uid}:{history_uid}"

    async def close(self) -> None:
        await self.cancel_active()
        if self._owns_http:
            await self._http.aclose()

    def request_interrupt(self) -> None:
        loop = self._active_loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(lambda: asyncio.create_task(self.cancel_active()))

    async def cancel_active(self) -> None:
        socket = self._active_socket
        generation_id = self._active_generation_id
        if socket is None or generation_id is None:
            return
        try:
            await socket.send(
                json.dumps(
                    {"type": "turn.cancel", "generation_id": generation_id},
                    ensure_ascii=False,
                )
            )
        except Exception:
            # The streaming task may already be closing the socket. Aria also guards
            # against committing a generation that was successfully cancelled.
            return

    async def stream_message(self, text: str) -> AsyncIterator[str]:
        if not text.strip():
            raise ValueError("message text cannot be empty")
        token = await self._access_token_value()
        self.last_agent_reply = None
        conversation = await self._conversation(token)
        connector = self._websocket_connector(self.config.websocket_url)
        async with connector as socket:
            self._active_socket = socket
            self._active_loop = asyncio.get_running_loop()
            emitted = False
            try:
                await self._send(socket, {"type": "authenticate", "access_token": token})
                await self._expect(socket, "auth.accepted")
                await self._send(
                    socket,
                    {
                        "type": "client_hello",
                        "cursors": {conversation.id: conversation.last_seq},
                    },
                )
                await self._expect(socket, "sync.completed")
                await self._send(
                    socket,
                    {
                        "type": "message.send",
                        "conversation_id": conversation.id,
                        "text": text,
                        "privacy_level": self.config.privacy_level,
                    },
                )
                while True:
                    event = await self._receive(socket)
                    event_type = event.get("type")
                    if event_type == "turn.accepted":
                        self._active_generation_id = _string(event.get("generation_id"))
                    elif event_type == "reply.delta":
                        delta = _string(_payload(event).get("delta"))
                        if delta:
                            emitted = True
                            yield delta
                    elif event_type == "reply.control":
                        reply = _payload(event).get("agent_reply")
                        if isinstance(reply, dict):
                            self.last_agent_reply = reply
                    elif event_type == "reply.committed":
                        message = _payload(event).get("message")
                        if not emitted and isinstance(message, dict):
                            content = _string(message.get("content"))
                            if content:
                                yield content
                        self._update_last_seq(conversation.id, event.get("seq"))
                        return
                    elif event_type == "turn.cancelled":
                        return
                    elif event_type in {"turn.failed", "protocol.error"}:
                        reason = _string(_payload(event).get("reason_code")) or event_type
                        raise AriaBridgeError(f"Aria stream failed: {reason}")
            except asyncio.CancelledError:
                with suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(self.cancel_active()), timeout=1.0)
                raise
            finally:
                self._active_socket = None
                self._active_generation_id = None
                self._active_loop = None

    async def _access_token_value(self) -> str:
        now = datetime.now(timezone.utc)  # noqa: UP017 -- Open-LLM-VTuber supports Python 3.10
        if (
            self._access_token is not None
            and self._token_expires_at is not None
            and self._token_expires_at > now + timedelta(seconds=30)
        ):
            return self._access_token
        password = os.environ.get(self.config.password_env)
        if not password:
            raise AriaBridgeError(
                f"Set the {self.config.password_env} environment variable "
                "before starting Open-LLM-VTuber"
            )
        response = await self._http.post("/api/v1/auth/login", json={"password": password})
        if response.status_code != 200:
            raise AriaBridgeError(f"Aria login failed with HTTP {response.status_code}")
        body = response.json()
        self._access_token = _string(body.get("access_token"))
        expires_at = _string(body.get("expires_at"))
        if not self._access_token or not expires_at:
            raise AriaBridgeError("Aria login response is missing token fields")
        self._token_expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        return self._access_token

    async def _conversation(self, token: str) -> _Conversation:
        headers = {"Authorization": f"Bearer {token}"}
        mapping = self._read_mapping()
        mapped_id = mapping.get(self._history_key)
        response = await self._http.get(
            "/api/v1/chat/conversations", params={"limit": 100}, headers=headers
        )
        if response.status_code == 401:
            self._access_token = None
            return await self._conversation(await self._access_token_value())
        response.raise_for_status()
        conversations = response.json()
        if mapped_id:
            for item in conversations:
                if item.get("id") == mapped_id:
                    return _Conversation(id=mapped_id, last_seq=int(item.get("last_seq", 0)))
        title = f"{self.config.conversation_title} · {self._history_key}"
        response = await self._http.post(
            "/api/v1/chat/conversations", json={"title": title[:240]}, headers=headers
        )
        response.raise_for_status()
        item = response.json()
        conversation_id = _string(item.get("id"))
        if not conversation_id:
            raise AriaBridgeError("Aria conversation response is missing id")
        mapping[self._history_key] = conversation_id
        self._write_mapping(mapping)
        return _Conversation(id=conversation_id, last_seq=int(item.get("last_seq", 0)))

    def _read_mapping(self) -> dict[str, str]:
        path = self.config.mapping_path.expanduser()
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(value, dict):
            return {}
        return {str(key): str(item) for key, item in value.items()}

    def _write_mapping(self, mapping: dict[str, str]) -> None:
        path = self.config.mapping_path.expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)

    def _update_last_seq(self, conversation_id: str, seq: object) -> None:
        # REST is queried at the start of every turn, so this is only a hook for
        # future persistent-connection optimization.
        del conversation_id, seq

    async def _expect(self, socket: WebSocketLike, event_type: str) -> dict[str, Any]:
        while True:
            event = await self._receive(socket)
            if event.get("type") == event_type:
                return event
            if event.get("type") == "protocol.error":
                reason = _string(_payload(event).get("reason_code"))
                raise AriaBridgeError(f"Aria protocol error: {reason}")

    @staticmethod
    async def _send(socket: WebSocketLike, frame: dict[str, object]) -> None:
        await socket.send(json.dumps(frame, ensure_ascii=False))

    @staticmethod
    async def _receive(socket: WebSocketLike) -> dict[str, Any]:
        raw = await socket.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise AriaBridgeError("Aria sent a non-object WebSocket frame")
        return value


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""
