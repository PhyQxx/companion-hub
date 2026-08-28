from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import Field, JsonValue, ValidationError

from app.auth import AuthService, ChatPrincipal, InvalidSession
from app.avatar import AvatarControlPublisher, control_from_agent_reply, with_reply_text
from app.chat import ChatService, MessageView, PendingTurn, TurnCancelled
from app.ids import uuid7
from app.llm import LLMRouteExhausted
from app.privacy import EgressBlocked
from app.runtime import TurnCoordinator
from app.schemas import PrivacyLevel
from app.schemas.common import StrictModel
from app.tools import ClientLocation, ClientLocationPayload

logger = logging.getLogger(__name__)


class AuthenticateFrame(StrictModel):
    type: Literal["authenticate"]
    access_token: Annotated[str, Field(min_length=20, max_length=512)]


class ClientHelloFrame(StrictModel):
    type: Literal["client_hello"]
    cursors: dict[UUID, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)


class SendFrame(StrictModel):
    type: Literal["message.send"]
    conversation_id: UUID
    text: Annotated[str, Field(min_length=1, max_length=20_000)]
    privacy_level: Literal["L0", "L1", "L2"] = "L1"
    # 可选终端 WGS84 临时位置: 帧内携带或复用连接缓存(TTL 15 分钟, 仅内存)。
    location: ClientLocationPayload | None = None


class CancelFrame(StrictModel):
    type: Literal["turn.cancel"]
    generation_id: UUID


class SyncFrame(StrictModel):
    type: Literal["sync.request"]
    conversation_id: UUID
    after_seq: Annotated[int, Field(ge=0)] = 0


@dataclass(slots=True)
class ChatConnection:
    websocket: WebSocket
    principal: ChatPrincipal
    subscriptions: set[UUID] = field(default_factory=set)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 连接级临时位置缓存: 不落库、不进日志, 失效后自动回落默认城市。
    location: ClientLocation | None = None

    def resolve_location(self, payload: ClientLocationPayload | None) -> ClientLocation | None:
        if payload is not None:
            self.location = payload.to_client_location()
        return self.location

    async def send(self, event: dict[str, object]) -> None:
        async with self.send_lock:
            await self.websocket.send_json(event)


class ChatWebSocketManager:
    def __init__(
        self,
        service: ChatService,
        turn_coordinator: TurnCoordinator | None = None,
        avatar_control_publisher: AvatarControlPublisher | None = None,
    ) -> None:
        self._service = service
        self._turns = turn_coordinator
        self._avatar_control = avatar_control_publisher
        self._connections: dict[int, ChatConnection] = {}
        self._tasks: dict[UUID, asyncio.Task[None]] = {}

    def connect(self, connection: ChatConnection) -> None:
        self._connections[id(connection)] = connection

    def disconnect(self, connection: ChatConnection) -> None:
        self._connections.pop(id(connection), None)

    async def subscribe(
        self, connection: ChatConnection, conversation_id: UUID, after_seq: int
    ) -> None:
        messages = await self._service.list_messages_after(
            conversation_id,
            user_id=connection.principal.user_id,
            after_seq=after_seq,
        )
        connection.subscriptions.add(conversation_id)
        for message in messages:
            await connection.send(_message_event(message))
        await connection.send(
            _event(
                conversation_id=conversation_id,
                event_type="sync.completed",
                seq=messages[-1].seq if messages else after_seq,
                payload={"after_seq": after_seq, "count": len(messages)},
            )
        )

    def start_generation(
        self,
        connection: ChatConnection,
        frame: SendFrame,
    ) -> None:
        task = asyncio.create_task(
            self._generate(connection, frame),
            name=f"chat-generation-{frame.conversation_id}",
        )
        task.add_done_callback(self._discard_finished_task)

    async def cancel(self, connection: ChatConnection, generation_id: UUID) -> None:
        changed = await self._service.cancel_turn(
            generation_id,
            user_id=connection.principal.user_id,
        )
        task = self._tasks.get(generation_id)
        if changed and task is not None:
            task.cancel()

    async def _generate(self, connection: ChatConnection, frame: SendFrame) -> None:
        pending: PendingTurn | None = None
        try:
            pending = await self._service.start_turn(
                frame.conversation_id,
                user_id=connection.principal.user_id,
                text=frame.text,
                privacy_level=PrivacyLevel(frame.privacy_level),
                client_location=connection.resolve_location(frame.location),
            )
            connection.subscriptions.add(frame.conversation_id)
            current_task = asyncio.current_task()
            if current_task is not None:
                self._tasks[pending.generation_id] = current_task
            # 状态机：accepted -> thinking
            if self._turns is not None:
                await self._turns.transition(pending.turn_id, 1, "thinking")
            await self.broadcast(
                connection.principal.user_id,
                frame.conversation_id,
                _message_event(pending.user_message, generation_id=pending.generation_id),
            )
            await self.broadcast(
                connection.principal.user_id,
                frame.conversation_id,
                _event(
                    conversation_id=frame.conversation_id,
                    event_type="turn.accepted",
                    seq=pending.user_message.seq,
                    generation_id=pending.generation_id,
                    payload={"turn_id": str(pending.turn_id), "turn_seq": pending.turn_seq},
                ),
            )
            delta_index = 0

            async def on_delta(delta: str) -> None:
                nonlocal delta_index
                delta_index += 1
                await self.broadcast(
                    connection.principal.user_id,
                    frame.conversation_id,
                    _event(
                        conversation_id=frame.conversation_id,
                        event_type="reply.delta",
                        seq=None,
                        generation_id=pending.generation_id,
                        payload={"delta": delta, "delta_index": delta_index},
                    ),
                )

            async def on_tool_event(tool_event: dict[str, object]) -> None:
                event_type = str(tool_event.get("type") or "tool.status")
                payload = {key: value for key, value in tool_event.items() if key != "type"}
                await self.broadcast(
                    connection.principal.user_id,
                    frame.conversation_id,
                    _event(
                        conversation_id=frame.conversation_id,
                        event_type=event_type,
                        seq=None,
                        generation_id=pending.generation_id,
                        payload=payload,
                    ),
                )

            # 状态机：thinking -> streaming
            if self._turns is not None:
                await self._turns.transition(pending.turn_id, 2, "streaming")
            turn = await self._service.run_stream(pending, on_delta, on_tool_event)
            reply_meta = (turn.assistant_message.decision_meta or {}).get("agent_reply")
            if isinstance(reply_meta, dict):
                await self.broadcast(
                    connection.principal.user_id,
                    frame.conversation_id,
                    _event(
                        conversation_id=frame.conversation_id,
                        event_type="reply.control",
                        seq=None,
                        generation_id=pending.generation_id,
                        payload={"agent_reply": reply_meta},
                    ),
                )
            control = control_from_agent_reply(reply_meta)
            if frame.privacy_level != "L2":
                control = with_reply_text(control, turn.assistant_message.content)
            if self._avatar_control is not None and control:
                await self._avatar_control.publish_avatar_control(
                    connection.principal.user_id, control
                )
            await self.broadcast(
                connection.principal.user_id,
                frame.conversation_id,
                _message_event(
                    turn.assistant_message,
                    event_type="reply.committed",
                    generation_id=pending.generation_id,
                ),
            )
            # 状态机：streaming -> completed
            if self._turns is not None:
                await self._turns.transition(pending.turn_id, 3, "completed")
        except (TurnCancelled, asyncio.CancelledError):
            if pending is not None:
                if self._turns is not None:
                    await self._turns.transition(
                        pending.turn_id, 2, "cancelled", reason="user_cancelled"
                    )
                await self.broadcast(
                    connection.principal.user_id,
                    frame.conversation_id,
                    _event(
                        conversation_id=frame.conversation_id,
                        event_type="turn.cancelled",
                        seq=None,
                        generation_id=pending.generation_id,
                        payload={"reason_code": "generation_cancelled"},
                    ),
                )
        except LLMRouteExhausted as error:
            if pending is not None and self._turns is not None:
                await self._turns.transition(pending.turn_id, 2, "failed")
            logger.error(
                "chat websocket model route failed conversation_id=%s generation_id=%s "
                "reason=%s failures=[%s]",
                frame.conversation_id,
                pending.generation_id if pending else None,
                error.reason_code,
                ", ".join(
                    f"{item.endpoint}#{item.attempt}:{item.error_type}" for item in error.failures
                ),
            )
            await self._send_failure(
                connection,
                frame,
                pending,
                error.reason_code,
            )
        except EgressBlocked as error:
            if pending is not None and self._turns is not None:
                await self._turns.transition(pending.turn_id, 2, "failed")
            logger.warning(
                "chat websocket model egress blocked conversation_id=%s generation_id=%s reason=%s",
                frame.conversation_id,
                pending.generation_id if pending else None,
                str(error),
            )
            await self._send_failure(
                connection,
                frame,
                pending,
                str(error),
            )
        except Exception:
            if pending is not None and self._turns is not None:
                await self._turns.transition(pending.turn_id, 2, "failed")
            logger.exception("chat generation failed for conversation %s", frame.conversation_id)
            await self._send_failure(connection, frame, pending, "generation_failed")
        finally:
            if pending is not None:
                self._tasks.pop(pending.generation_id, None)

    async def _send_failure(
        self,
        connection: ChatConnection,
        frame: SendFrame,
        pending: PendingTurn | None,
        reason_code: str,
    ) -> None:
        event = _event(
            conversation_id=frame.conversation_id,
            event_type="turn.failed",
            seq=None,
            generation_id=pending.generation_id if pending else None,
            payload={"reason_code": reason_code},
        )
        if frame.conversation_id not in connection.subscriptions:
            await connection.send(event)
            return
        await self.broadcast(
            connection.principal.user_id,
            frame.conversation_id,
            event,
        )

    async def broadcast(
        self, user_id: UUID, conversation_id: UUID, event: dict[str, object]
    ) -> None:
        connections = [
            connection
            for connection in self._connections.values()
            if connection.principal.user_id == user_id
            and conversation_id in connection.subscriptions
        ]
        results = await asyncio.gather(
            *(connection.send(event) for connection in connections),
            return_exceptions=True,
        )
        for connection, result in zip(connections, results, strict=True):
            if isinstance(result, Exception):
                self.disconnect(connection)

    async def broadcast_proactive(self, user_id: UUID, message: MessageView) -> None:
        await self.broadcast(
            user_id,
            message.conversation_id,
            _message_event(message, event_type="proactive.committed"),
        )

    async def submit_device_message(
        self, user_id: UUID, text: str, privacy_level: PrivacyLevel
    ) -> tuple[dict[str, JsonValue], str]:
        conversations = await self._service.list_conversations(user_id=user_id, limit=20)
        conversation = next(
            (item for item in conversations if item.status == "active"), None
        )
        if conversation is None:
            conversation = await self._service.create_conversation(
                user_id=user_id, title="桌宠对话"
            )
        turn = await self._service.send_message(
            conversation.id,
            user_id=user_id,
            text=text,
            privacy_level=privacy_level,
        )
        await self.broadcast(
            user_id,
            conversation.id,
            _message_event(turn.user_message),
        )
        await self.broadcast(
            user_id,
            conversation.id,
            _message_event(turn.assistant_message, event_type="reply.committed"),
        )
        reply_meta = (turn.assistant_message.decision_meta or {}).get("agent_reply")
        control = control_from_agent_reply(reply_meta)
        if privacy_level in {PrivacyLevel.L0, PrivacyLevel.L1}:
            control = with_reply_text(control, turn.assistant_message.content)
        if self._avatar_control is not None and control:
            await self._avatar_control.publish_avatar_control(user_id, control)
        speech_text = turn.assistant_message.content
        if isinstance(reply_meta, dict):
            configured_tts = reply_meta.get("tts_text")
            if isinstance(configured_tts, str) and configured_tts.strip():
                speech_text = configured_tts.strip()
        return (
            {
                "conversation_id": str(conversation.id),
                "message_id": str(turn.assistant_message.id),
            },
            speech_text,
        )

    def _discard_finished_task(self, task: asyncio.Task[None]) -> None:
        with suppress(asyncio.CancelledError):
            task.exception()


def create_chat_websocket_router(
    service: ChatService,
    auth_service: AuthService,
    turn_coordinator: TurnCoordinator | None = None,
    avatar_control_publisher: AvatarControlPublisher | None = None,
) -> tuple[APIRouter, ChatWebSocketManager]:
    router = APIRouter(tags=["chat-websocket"])
    manager = ChatWebSocketManager(
        service,
        turn_coordinator=turn_coordinator,
        avatar_control_publisher=avatar_control_publisher,
    )

    @router.websocket("/ws/chat")
    async def chat_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            async with asyncio.timeout(5):
                raw_auth = await websocket.receive_json()
            auth = AuthenticateFrame.model_validate(raw_auth)
            principal = await auth_service.authenticate(auth.access_token)
        except WebSocketDisconnect:
            # 浏览器在鉴权帧发送前主动关闭连接是正常的生命周期事件。
            # 此时连接已经不可写, 不能再发送 4401 close, 否则 Starlette
            # 会再次抛 WebSocketDisconnect/ClientDisconnected 并污染服务日志。
            return
        except (TimeoutError, ValidationError, InvalidSession):
            with suppress(WebSocketDisconnect):
                await websocket.close(code=4401, reason="authentication required")
            return
        connection = ChatConnection(websocket=websocket, principal=principal)
        manager.connect(connection)
        await connection.send(
            _event(
                conversation_id=None,
                event_type="auth.accepted",
                seq=None,
                payload={
                    "user_id": str(principal.user_id),
                    "expires_at": principal.expires_at.isoformat(),
                },
            )
        )
        try:
            while True:
                raw = await websocket.receive_json()
                try:
                    connection.principal = await auth_service.authenticate(auth.access_token)
                except InvalidSession:
                    await websocket.close(code=4401, reason="session expired")
                    break
                await _handle_frame(manager, connection, raw)
        except WebSocketDisconnect:
            pass
        finally:
            manager.disconnect(connection)

    return router, manager


async def _handle_frame(
    manager: ChatWebSocketManager,
    connection: ChatConnection,
    raw: Any,
) -> None:
    try:
        frame_type = raw.get("type") if isinstance(raw, dict) else None
        if frame_type == "client_hello":
            hello_frame = ClientHelloFrame.model_validate(raw)
            for conversation_id, after_seq in hello_frame.cursors.items():
                await manager.subscribe(connection, conversation_id, after_seq)
        elif frame_type == "sync.request":
            sync_frame = SyncFrame.model_validate(raw)
            await manager.subscribe(connection, sync_frame.conversation_id, sync_frame.after_seq)
        elif frame_type == "message.send":
            manager.start_generation(connection, SendFrame.model_validate(raw))
        elif frame_type == "turn.cancel":
            cancel_frame = CancelFrame.model_validate(raw)
            await manager.cancel(connection, cancel_frame.generation_id)
        elif frame_type == "ping":
            await connection.send(
                _event(
                    conversation_id=None,
                    event_type="pong",
                    seq=None,
                    payload={},
                )
            )
        else:
            raise ValueError("unknown frame type")
    except (ValidationError, ValueError, LookupError) as error:
        await connection.send(
            _event(
                conversation_id=None,
                event_type="protocol.error",
                seq=None,
                payload={"reason_code": type(error).__name__},
            )
        )


def _message_event(
    message: MessageView,
    *,
    event_type: str | None = None,
    generation_id: UUID | None = None,
) -> dict[str, object]:
    resolved_type = event_type or (
        "reply.committed" if message.role == "assistant" else "message.committed"
    )
    return _event(
        conversation_id=message.conversation_id,
        event_type=resolved_type,
        seq=message.seq,
        generation_id=generation_id or message.generation_id,
        payload={
            "message": {
                "id": str(message.id),
                "turn_id": str(message.turn_id),
                "seq": message.seq,
                "role": message.role,
                "content": message.content,
                "privacy_level": message.privacy_level,
                "generation_id": str(message.generation_id) if message.generation_id else None,
                "decision_meta": message.decision_meta,
                "created_at": message.created_at.isoformat(),
            }
        },
    )


def _event(
    *,
    conversation_id: UUID | None,
    event_type: str,
    seq: int | None,
    payload: dict[str, object],
    generation_id: UUID | None = None,
) -> dict[str, object]:
    return {
        "proto_version": 1,
        "stream": f"conversation:{conversation_id}" if conversation_id else "control",
        "seq": seq,
        "event_id": str(uuid7()),
        "type": event_type,
        "generation_id": str(generation_id) if generation_id else None,
        "sent_at": datetime.now(UTC).isoformat(),
        "payload": payload,
    }
