from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select

from app.cognition import CognitiveDecision, SemanticEvent
from app.db import AppUserRecord, Database
from app.ids import uuid7
from app.perception import PerceptionPipeline, PerceptionResult
from app.schemas import EphemeralSignal, PrivacyLevel
from app.schemas.common import TelemetryPart

PresenceDeliver = Callable[..., Awaitable[Any]]


class PresencePipeline(Protocol):
    async def process(self, event: SemanticEvent) -> PerceptionResult: ...


class MqttPresenceBridge:
    """把 L3 MQTT 原始存在信号稳定化为可审计的 L1 语义事件。"""

    def __init__(
        self,
        database: Database,
        pipeline: PerceptionPipeline | PresencePipeline,
        *,
        proactive_deliver: PresenceDeliver | None = None,
        stable_seconds: float = 5.0,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._pipeline = pipeline
        self._proactive_deliver = proactive_deliver
        self._stable_seconds = stable_seconds
        self._sleep = sleeper or asyncio.sleep
        self._clock = clock or (lambda: datetime.now(UTC))
        self._latest: dict[str, bool] = {}
        self._confirmed: dict[str, bool] = {}
        self._pending: dict[str, asyncio.Task[None]] = {}

    async def handle(self, signal: EphemeralSignal) -> None:
        if signal.source.adapter_id != "device.mqtt" or signal.channel != "presence":
            return
        if not isinstance(signal.content, TelemetryPart):
            return
        state = _presence_value(signal.content.value)
        if state is None:
            return
        device_id = signal.source.endpoint_id
        self._latest[device_id] = state
        pending = self._pending.pop(device_id, None)
        if pending is not None:
            pending.cancel()
        if device_id not in self._confirmed:
            # retained/startup message only establishes a baseline; it must not announce an arrival.
            self._confirmed[device_id] = state
            return
        if self._confirmed[device_id] == state:
            return
        task = asyncio.create_task(
            self._stabilize(device_id, state, signal),
            name=f"mqtt-presence-{device_id}",
        )
        self._pending[device_id] = task
        task.add_done_callback(partial(self._discard, device_id))

    async def stop(self) -> None:
        tasks = tuple(self._pending.values())
        self._pending.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def wait_pending(self) -> None:
        tasks = tuple(self._pending.values())
        if tasks:
            await asyncio.gather(*tasks)

    async def _stabilize(
        self, device_id: str, expected: bool, signal: EphemeralSignal
    ) -> None:
        await self._sleep(self._stable_seconds)
        if self._latest.get(device_id) != expected:
            return
        if self._confirmed.get(device_id) == expected:
            return
        owner = await self._owner()
        if owner is None:
            return
        self._confirmed[device_id] = expected
        now = self._clock()
        state_text = "检测到稳定存在" if expected else "检测到稳定离开"
        event = SemanticEvent(
            event_id=uuid7(),
            user_id=owner,
            kind="presence.changed",
            source_kind="mqtt_presence",
            dedupe_key=f"mqtt-presence:{device_id}:{'present' if expected else 'absent'}",
            summary=f"{device_id} {state_text}",
            occurred_at=now,
            privacy_level=PrivacyLevel.L1,
            confidence=0.9,
            evidence_ids=[str(signal.signal_id)],
            attributes={
                "device_id": device_id,
                "presence": "present" if expected else "absent",
                "message": f"{device_id} {state_text}。",
                "salience": 0.65 if expected else 0.5,
            },
            expires_at=now + timedelta(minutes=5),
        )
        result = await self._pipeline.process(event)
        await self._deliver(event, result.decision)

    async def _owner(self) -> UUID | None:
        async with self._database.sessions() as session:
            owner = await session.scalar(
                select(AppUserRecord.id)
                .where(AppUserRecord.status == "active")
                .order_by(AppUserRecord.created_at)
            )
        return UUID(str(owner)) if owner is not None else None

    async def _deliver(
        self, event: SemanticEvent, decision: CognitiveDecision | None
    ) -> None:
        if self._proactive_deliver is None or decision is None:
            return
        if decision.decision not in {"inform", "suggest", "ask", "escalate"}:
            return
        await self._proactive_deliver(
            decision.message or event.summary,
            entity_id=str(event.attributes.get("device_id", "mqtt-presence")),
            rule_id="perception_mqtt_presence",
            trigger_kind=event.kind,
            privacy_level=event.privacy_level,
            cognitive_decision=decision,
            target_user_id=event.user_id,
        )

    def _discard(self, device_id: str, completed: asyncio.Task[None]) -> None:
        if self._pending.get(device_id) is completed:
            self._pending.pop(device_id, None)


def _presence_value(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"present", "detected", "on", "home", "true", "1"}:
            return True
        if normalized in {"absent", "clear", "off", "away", "false", "0"}:
            return False
    return None
