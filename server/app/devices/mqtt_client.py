from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.ids import uuid7
from app.schemas import EphemeralSignal, PrivacyLevel
from app.schemas.common import SourceRef

try:
    import aiomqtt
except ImportError:
    aiomqtt = None  # type: ignore[assignment]

logger = logging.getLogger("app.devices.mqtt")


@dataclass(frozen=True, slots=True)
class MqttTelemetryMessage:
    device_id: str
    sensor_type: str
    value: Any
    timestamp: datetime
    raw_topic: str


class MqttTelemetryBuffer:
    """MQTT 遥测内存缓存：按 sensor_key 保存最近 N 条读数，支持 TTL 去抖。"""

    def __init__(self, max_per_sensor: int = 100, ttl_seconds: float = 5.0) -> None:
        self._buffers: dict[str, deque[dict[str, Any]]] = {}
        self._last_seen: dict[str, datetime] = {}
        self._max_per_sensor = max_per_sensor
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()

    async def push(self, msg: MqttTelemetryMessage) -> None:
        key = f"{msg.device_id}:{msg.sensor_type}"
        record = {
            "value": msg.value,
            "timestamp": msg.timestamp.isoformat(),
        }
        async with self._lock:
            if key not in self._buffers:
                self._buffers[key] = deque(maxlen=self._max_per_sensor)
            self._buffers[key].append(record)
            self._last_seen[key] = msg.timestamp

    async def latest(self, device_id: str, sensor_type: str) -> dict[str, Any] | None:
        key = f"{device_id}:{sensor_type}"
        async with self._lock:
            buf = self._buffers.get(key)
            if not buf:
                return None
            return dict(buf[-1])

    async def latest_all(self, device_id: str | None = None) -> dict[str, dict[str, Any]]:
        async with self._lock:
            result: dict[str, dict[str, Any]] = {}
            for key, buf in self._buffers.items():
                if device_id is not None and not key.startswith(f"{device_id}:"):
                    continue
                if buf:
                    result[key] = dict(buf[-1])
            return result

    async def is_fresh(
        self, device_id: str, sensor_type: str, max_age_seconds: float = 300.0
    ) -> bool:
        key = f"{device_id}:{sensor_type}"
        async with self._lock:
            last = self._last_seen.get(key)
            if last is None:
                return False
            return (datetime.now(UTC) - last).total_seconds() <= max_age_seconds

    async def to_ephemeral_signals(self, max_age_seconds: float = 60.0) -> list[EphemeralSignal]:
        now = datetime.now(UTC)
        signals: list[EphemeralSignal] = []
        async with self._lock:
            for key, buf in self._buffers.items():
                if not buf:
                    continue
                last = buf[-1]
                ts = datetime.fromisoformat(last["timestamp"])
                if (now - ts).total_seconds() > max_age_seconds:
                    continue
                device_id, sensor_type = key.split(":", 1)
                signals.append(
                    EphemeralSignal(
                        signal_id=uuid7(),
                        source=SourceRef(
                            adapter_id="device.mqtt",
                            adapter_instance_id=uuid7(),
                            endpoint_id=device_id,
                        ),
                        channel=sensor_type,
                        occurred_at=ts,
                        privacy_level=PrivacyLevel.L3,
                        content={
                            "type": "telemetry",
                            "channel": sensor_type,
                            "value": last["value"],
                        },
                        expires_at=now + timedelta(seconds=1),
                    )
                )
        return signals


class MqttDeviceClient:
    """Hub 侧 MQTT 客户端：订阅设备遥测主题，将消息写入内存缓存和 EphemeralSignal。"""

    def __init__(
        self,
        *,
        host: str = "localhost",
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        telemetry_buffer: MqttTelemetryBuffer | None = None,
        on_signal: Callable[[EphemeralSignal], Awaitable[None] | None] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._buffer = telemetry_buffer or MqttTelemetryBuffer()
        self._on_signal = on_signal
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def buffer(self) -> MqttTelemetryBuffer:
        return self._buffer

    def set_signal_handler(
        self,
        handler: Callable[[EphemeralSignal], Awaitable[None] | None] | None,
    ) -> None:
        self._on_signal = handler

    async def start(self) -> None:
        if aiomqtt is None:
            logger.warning("aiomqtt is not installed; MQTT device channel is disabled")
            return
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="aria-mqtt-client")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                async with aiomqtt.Client(
                    hostname=self._host,
                    port=self._port,
                    username=self._username,
                    password=self._password,
                ) as client:
                    await client.subscribe("hub/devices/+/telemetry")
                    logger.info("MQTT subscribed to hub/devices/+/telemetry")
                    async for message in client.messages:
                        if self._stop.is_set():
                            break
                        await self._handle(message)
            except aiomqtt.MqttError as error:
                logger.warning("MQTT connection error: %s; retry in 5s", error)
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=5.0)
            except Exception:
                logger.exception("MQTT unexpected error; retry in 5s")
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=5.0)

    async def _handle(self, message: aiomqtt.Message) -> None:
        try:
            topic = str(message.topic)
            payload = json.loads(message.payload.decode())
            parts = topic.split("/")
            if len(parts) < 4:
                return
            device_id = parts[2]
            sensor_type = payload.get("sensor_type", "unknown")
            value = payload.get("value")
            ts_raw = payload.get("timestamp")
            timestamp = datetime.fromisoformat(ts_raw) if ts_raw else datetime.now(UTC)
            msg = MqttTelemetryMessage(
                device_id=device_id,
                sensor_type=sensor_type,
                value=value,
                timestamp=timestamp,
                raw_topic=topic,
            )
            await self._buffer.push(msg)
            if self._on_signal is not None:
                now = datetime.now(UTC)
                signal = EphemeralSignal(
                    signal_id=uuid7(),
                    source=SourceRef(
                        adapter_id="device.mqtt",
                        adapter_instance_id=uuid7(),
                        endpoint_id=device_id,
                    ),
                    channel=sensor_type,
                    occurred_at=timestamp if timestamp < now else now - timedelta(milliseconds=1),
                    privacy_level=PrivacyLevel.L3,
                    content={"type": "telemetry", "channel": sensor_type, "value": value},
                    expires_at=now + timedelta(seconds=1),
                )
                maybe = self._on_signal(signal)
                if maybe is not None:
                    await maybe
        except Exception:
            logger.exception("MQTT message handling failed for topic %s", message.topic)
