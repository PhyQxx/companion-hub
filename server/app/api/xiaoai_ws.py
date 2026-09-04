"""XiaoAI text-terminal adapter.

The consumer XiaoAI speaker keeps ownership of wake word, ASR, and playback.
This endpoint only accepts the recognized text and streams speech-ready sentences
back to a trusted, separately deployed gateway.  Xiaomi credentials therefore
never enter the Hub process.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections import OrderedDict
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import Field, ValidationError

from app.chat import ChatService, PendingTurn, TurnCancelled
from app.llm import LLMRoute, LLMRouteExhausted
from app.privacy import EgressBlocked
from app.schemas.common import PrivacyLevel, StrictModel
from app.voice import MarkdownSpeechFilter, SentenceBuffer

logger = logging.getLogger(__name__)


class XiaoAiAuthenticateFrame(StrictModel):
    type: Literal["authenticate"]
    gateway_token: Annotated[str, Field(min_length=20, max_length=512)]


class XiaoAiHelloFrame(StrictModel):
    type: Literal["xiaoai.hello"]
    did: Annotated[str, Field(min_length=1, max_length=160)]
    display_name: Annotated[str, Field(min_length=1, max_length=160)] = "小爱音箱"
    model: Annotated[str | None, Field(max_length=160)] = None
    conversation_id: UUID | None = None
    capabilities: dict[str, bool] = Field(default_factory=dict)


class XiaoAiQueryFrame(StrictModel):
    type: Literal["xiaoai.query"]
    event_id: Annotated[str, Field(min_length=1, max_length=256)]
    text: Annotated[str, Field(min_length=1, max_length=20_000)]
    privacy_level: Literal["L0", "L1"] = "L1"


class XiaoAiCancelFrame(StrictModel):
    type: Literal["xiaoai.cancel"]
    generation_id: UUID | None = None


@dataclass(slots=True)
class XiaoAiConnection:
    websocket: WebSocket
    owner_user_id: UUID
    did: str | None = None
    display_name: str = "小爱音箱"
    conversation_id: UUID | None = None
    generation_id: UUID | None = None
    turn_task: asyncio.Task[None] | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, event_type: str, **payload: object) -> None:
        async with self.send_lock:
            await self.websocket.send_json({"type": event_type, **payload})


class XiaoAiWebSocketManager:
    """Own XiaoAI gateway sessions while ChatService owns all durable state."""

    def __init__(
        self,
        service: ChatService,
        *,
        dedupe_limit: int = 2_000,
    ) -> None:
        self._service = service
        self._dedupe_limit = dedupe_limit
        self._seen_events: OrderedDict[str, None] = OrderedDict()

    def connect(self, websocket: WebSocket, *, owner_user_id: UUID) -> XiaoAiConnection:
        return XiaoAiConnection(websocket=websocket, owner_user_id=owner_user_id)

    async def disconnect(self, connection: XiaoAiConnection) -> None:
        await self.cancel(connection)

    async def handle(self, connection: XiaoAiConnection, raw: Any) -> None:
        frame_type = raw.get("type") if isinstance(raw, dict) else None
        try:
            if frame_type == "xiaoai.hello":
                await self._hello(connection, XiaoAiHelloFrame.model_validate(raw))
            elif frame_type == "xiaoai.query":
                await self._query(connection, XiaoAiQueryFrame.model_validate(raw))
            elif frame_type == "xiaoai.cancel":
                frame = XiaoAiCancelFrame.model_validate(raw)
                await self.cancel(connection, frame.generation_id)
            else:
                await connection.send("xiaoai.error", reason_code="unsupported_frame")
        except ValidationError as error:
            await connection.send(
                "xiaoai.error",
                reason_code="invalid_frame",
                detail=error.errors(include_url=False),
            )
        except LookupError:
            await connection.send("xiaoai.error", reason_code="conversation_not_found")

    async def _hello(self, connection: XiaoAiConnection, frame: XiaoAiHelloFrame) -> None:
        if connection.did is not None:
            await connection.send("xiaoai.error", reason_code="hello_already_received")
            return
        conversation_id = frame.conversation_id
        if conversation_id is None:
            conversation = await self._service.create_conversation(
                user_id=connection.owner_user_id,
                title=f"小爱 · {frame.display_name}",
            )
            conversation_id = conversation.id
        else:
            # Verify ownership without exposing conversation contents to the gateway.
            try:
                await self._service.list_messages(
                    conversation_id, user_id=connection.owner_user_id, limit=1
                )
            except LookupError:
                # The gateway may retain its state volume after the Hub database
                # is restored. Recover by binding a fresh conversation.
                conversation = await self._service.create_conversation(
                    user_id=connection.owner_user_id,
                    title=f"小爱 · {frame.display_name}",
                )
                conversation_id = conversation.id
        connection.did = frame.did
        connection.display_name = frame.display_name
        connection.conversation_id = conversation_id
        await connection.send(
            "xiaoai.ready",
            endpoint_id=f"xiaoai:{frame.did}",
            conversation_id=str(conversation_id),
            privacy_max="L1",
        )

    async def _query(self, connection: XiaoAiConnection, frame: XiaoAiQueryFrame) -> None:
        if connection.did is None or connection.conversation_id is None:
            await connection.send("xiaoai.error", reason_code="hello_required")
            return
        if not frame.text.strip():
            await connection.send("xiaoai.error", reason_code="invalid_text")
            return
        if not self._claim_event(connection.did, frame.event_id):
            await connection.send(
                "xiaoai.query.duplicate",
                event_id=frame.event_id,
            )
            return
        await self.cancel(connection)
        connection.turn_task = asyncio.create_task(
            self._generate(connection, frame),
            name=f"xiaoai-turn-{connection.did}",
        )

    async def cancel(self, connection: XiaoAiConnection, generation_id: UUID | None = None) -> None:
        active_generation = connection.generation_id
        if generation_id is not None and active_generation != generation_id:
            return
        if active_generation is not None:
            await self._service.cancel_turn(active_generation, user_id=connection.owner_user_id)
        task = connection.turn_task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        connection.turn_task = None
        connection.generation_id = None

    async def _generate(self, connection: XiaoAiConnection, frame: XiaoAiQueryFrame) -> None:
        pending: PendingTurn | None = None
        buffer = SentenceBuffer()
        speech_filter = MarkdownSpeechFilter()
        sentence_index = 0

        async def emit_sentence(sentence: str) -> None:
            nonlocal sentence_index
            clean = speech_filter.clean(sentence)
            if not clean or pending is None:
                return
            await connection.send(
                "reply.sentence",
                event_id=frame.event_id,
                generation_id=str(pending.generation_id),
                index=sentence_index,
                text=clean,
            )
            sentence_index += 1

        try:
            assert connection.conversation_id is not None
            pending = await self._service.start_turn(
                connection.conversation_id,
                user_id=connection.owner_user_id,
                text=frame.text.strip(),
                privacy_level=PrivacyLevel(frame.privacy_level),
                max_context_messages=8,
                llm_route=LLMRoute.VOICE,
            )
            connection.generation_id = pending.generation_id
            await connection.send(
                "turn.accepted",
                event_id=frame.event_id,
                generation_id=str(pending.generation_id),
                turn_id=str(pending.turn_id),
                conversation_id=str(pending.conversation_id),
            )

            async def on_delta(delta: str) -> None:
                for sentence in buffer.push(delta):
                    await emit_sentence(sentence)

            async def on_tool_event(event: dict[str, object]) -> None:
                await connection.send(
                    str(event.get("type") or "tool.status"),
                    generation_id=str(pending.generation_id),
                    **{key: value for key, value in event.items() if key != "type"},
                )

            turn = await self._service.run_stream(pending, on_delta, on_tool_event)
            remainder = buffer.flush()
            if remainder:
                await emit_sentence(remainder)
            await connection.send(
                "reply.committed",
                event_id=frame.event_id,
                generation_id=str(pending.generation_id),
                message_id=str(turn.assistant_message.id),
                sentence_count=sentence_index,
            )
        except (TurnCancelled, asyncio.CancelledError):
            if pending is not None:
                with suppress(WebSocketDisconnect, RuntimeError):
                    await connection.send(
                        "turn.cancelled",
                        event_id=frame.event_id,
                        generation_id=str(pending.generation_id),
                        reason_code="generation_cancelled",
                    )
        except LookupError:
            await connection.send("turn.failed", reason_code="conversation_not_found")
        except LLMRouteExhausted as error:
            await connection.send("turn.failed", reason_code=error.reason_code)
        except EgressBlocked as error:
            await connection.send("turn.failed", reason_code=str(error))
        except Exception:
            logger.exception("XiaoAI turn failed for did=%s", connection.did)
            with suppress(WebSocketDisconnect, RuntimeError):
                await connection.send("turn.failed", reason_code="generation_failed")
        finally:
            if connection.turn_task is asyncio.current_task():
                connection.turn_task = None
                connection.generation_id = None

    def _claim_event(self, did: str, event_id: str) -> bool:
        key = f"{did}:{event_id}"
        if key in self._seen_events:
            self._seen_events.move_to_end(key)
            return False
        self._seen_events[key] = None
        while len(self._seen_events) > self._dedupe_limit:
            self._seen_events.popitem(last=False)
        return True


def create_xiaoai_websocket_router(
    service: ChatService,
    *,
    gateway_token: str | None = None,
    owner_user_id: UUID | None = None,
    credentials_provider: Callable[[], tuple[str, UUID] | None] | None = None,
) -> tuple[APIRouter, XiaoAiWebSocketManager]:
    router = APIRouter(tags=["xiaoai-adapter"])
    manager = XiaoAiWebSocketManager(service)

    @router.websocket("/ws/adapters/xiaoai")
    async def xiaoai_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            credentials = (
                credentials_provider()
                if credentials_provider is not None
                else ((gateway_token, owner_user_id) if gateway_token and owner_user_id else None)
            )
            if credentials is None:
                raise ValueError("xiaoai disabled")
            expected_token, current_owner_user_id = credentials
            async with asyncio.timeout(5):
                raw_auth = await websocket.receive_json()
            auth = XiaoAiAuthenticateFrame.model_validate(raw_auth)
            if not secrets.compare_digest(auth.gateway_token, expected_token):
                raise ValueError("invalid gateway token")
        except WebSocketDisconnect:
            return
        except (TimeoutError, ValidationError, ValueError):
            await websocket.close(code=4401, reason="gateway authentication required")
            return

        connection = manager.connect(websocket, owner_user_id=current_owner_user_id)
        await connection.send("auth.accepted")
        try:
            while True:
                await manager.handle(connection, await websocket.receive_json())
        except WebSocketDisconnect:
            pass
        finally:
            await manager.disconnect(connection)

    return router, manager
