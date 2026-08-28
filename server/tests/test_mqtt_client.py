from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from app.cognition import CognitiveDecision, DecisionKind, SemanticEvent, Urgency
from app.db import AppUserRecord, Base, Database, create_database
from app.devices import MqttPresenceBridge
from app.devices.mqtt_client import MqttDeviceClient, MqttTelemetryBuffer, MqttTelemetryMessage
from app.ids import uuid7
from app.perception import PerceptionDisposition, PerceptionResult
from app.schemas import EphemeralSignal, PrivacyLevel
from app.schemas.common import SourceRef


@pytest.fixture
def buffer() -> MqttTelemetryBuffer:
    return MqttTelemetryBuffer(max_per_sensor=5, ttl_seconds=5.0)


@pytest.fixture
def sample_msg() -> MqttTelemetryMessage:
    return MqttTelemetryMessage(
        device_id="esp32_001",
        sensor_type="presence",
        value=True,
        timestamp=datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC),
        raw_topic="hub/devices/esp32_001/telemetry",
    )


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
async def user_id(database: Database) -> UUID:
    value = uuid7()
    async with database.sessions.begin() as session:
        session.add(AppUserRecord(id=value, display_name="Presence", status="active"))
    return value


def presence_signal(value: object, *, device_id: str = "radar_01") -> EphemeralSignal:
    now = datetime.now(UTC)
    return EphemeralSignal(
        signal_id=uuid7(),
        source=SourceRef(
            adapter_id="device.mqtt",
            adapter_instance_id=uuid7(),
            endpoint_id=device_id,
        ),
        channel="presence",
        occurred_at=now,
        privacy_level=PrivacyLevel.L3,
        content={"type": "telemetry", "channel": "presence", "value": value},
        expires_at=now + timedelta(seconds=30),
    )


class FakePresencePipeline:
    def __init__(self) -> None:
        self.events: list[SemanticEvent] = []

    async def process(self, event: SemanticEvent) -> PerceptionResult:
        self.events.append(event)
        decision = CognitiveDecision(
            id=uuid7(),
            event_id=event.event_id,
            user_id=event.user_id,
            trigger_kind=event.kind,
            decision=DecisionKind.INFORM,
            reason_codes=["presence_confirmed"],
            evidence_ids=event.evidence_ids,
            confidence=event.confidence,
            urgency=Urgency.NORMAL,
            attention_score=0.65,
            policy_version="test",
            message=event.summary,
            created_at=datetime.now(UTC),
        )
        return PerceptionResult(
            event_id=event.event_id,
            disposition=PerceptionDisposition.PROCESSED,
            decision=decision,
        )


class TestMqttTelemetryBuffer:
    async def test_push_and_latest(
        self, buffer: MqttTelemetryBuffer, sample_msg: MqttTelemetryMessage
    ) -> None:
        await buffer.push(sample_msg)
        latest = await buffer.latest("esp32_001", "presence")
        assert latest is not None
        assert latest["value"] is True
        assert latest["timestamp"] == "2026-08-17T10:00:00+00:00"

    async def test_latest_missing_returns_none(self, buffer: MqttTelemetryBuffer) -> None:
        assert await buffer.latest("none", "none") is None

    async def test_buffer_maxlen_eviction(self, buffer: MqttTelemetryBuffer) -> None:
        for i in range(10):
            msg = MqttTelemetryMessage(
                device_id="dev",
                sensor_type="temp",
                value=i,
                timestamp=datetime(2026, 8, 17, 10, 0, i, tzinfo=UTC),
                raw_topic="hub/devices/dev/telemetry",
            )
            await buffer.push(msg)
        latest = await buffer.latest("dev", "temp")
        assert latest is not None
        assert latest["value"] == 9
        all_items = await buffer.latest_all(device_id="dev")
        assert len(all_items) == 1
        # latest_all returns dict of latest records, not the deque itself
        assert all_items["dev:temp"]["value"] == 9

    async def test_is_fresh(self, buffer: MqttTelemetryBuffer) -> None:
        now = datetime.now(UTC)
        msg = MqttTelemetryMessage(
            device_id="esp32_001",
            sensor_type="presence",
            value=True,
            timestamp=now,
            raw_topic="hub/devices/esp32_001/telemetry",
        )
        await buffer.push(msg)
        assert await buffer.is_fresh("esp32_001", "presence", max_age_seconds=300)

    async def test_is_fresh_expired(self, buffer: MqttTelemetryBuffer) -> None:
        old_msg = MqttTelemetryMessage(
            device_id="dev",
            sensor_type="temp",
            value=20,
            timestamp=datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC),
            raw_topic="hub/devices/dev/telemetry",
        )
        await buffer.push(old_msg)
        assert not await buffer.is_fresh("dev", "temp", max_age_seconds=300)

    async def test_latest_all_filter_by_device(self, buffer: MqttTelemetryBuffer) -> None:
        msg1 = MqttTelemetryMessage(
            device_id="dev_a", sensor_type="temp", value=20,
            timestamp=datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC),
            raw_topic="hub/devices/dev_a/telemetry",
        )
        msg2 = MqttTelemetryMessage(
            device_id="dev_b", sensor_type="humidity", value=60,
            timestamp=datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC),
            raw_topic="hub/devices/dev_b/telemetry",
        )
        await buffer.push(msg1)
        await buffer.push(msg2)
        all_a = await buffer.latest_all(device_id="dev_a")
        assert len(all_a) == 1
        assert "dev_a:temp" in all_a

    async def test_to_ephemeral_signals(self, buffer: MqttTelemetryBuffer) -> None:
        now = datetime.now(UTC)
        msg = MqttTelemetryMessage(
            device_id="radar_01",
            sensor_type="presence",
            value=True,
            timestamp=now,
            raw_topic="hub/devices/radar_01/telemetry",
        )
        await buffer.push(msg)
        signals = await buffer.to_ephemeral_signals(max_age_seconds=60)
        assert len(signals) == 1
        assert signals[0].channel == "presence"
        assert signals[0].source.adapter_id == "device.mqtt"
        assert signals[0].source.endpoint_id == "radar_01"
        assert signals[0].privacy_level == PrivacyLevel.L3

    async def test_to_ephemeral_signals_skips_stale(self, buffer: MqttTelemetryBuffer) -> None:
        old = MqttTelemetryMessage(
            device_id="radar_01", sensor_type="presence", value=True,
            timestamp=datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC),
            raw_topic="hub/devices/radar_01/telemetry",
        )
        await buffer.push(old)
        signals = await buffer.to_ephemeral_signals(max_age_seconds=60)
        assert len(signals) == 0


class TestMqttDeviceClient:
    async def test_start_without_aiomqtt_warns(self) -> None:
        client = MqttDeviceClient(host="localhost", port=1883)
        with patch("app.devices.mqtt_client.aiomqtt", None):
            await client.start()
        assert client._task is None

    async def test_start_stop_lifecycle(self) -> None:
        client = MqttDeviceClient(host="localhost", port=1883)
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.subscribe = AsyncMock()
        mock_client.messages = AsyncMock()
        mock_client.messages.__aiter__ = MagicMock(return_value=iter([]))

        with patch("app.devices.mqtt_client.aiomqtt") as mock_aiomqtt:
            mock_aiomqtt.Client = MagicMock(return_value=mock_client)
            await client.start()
            assert client._task is not None
            await client.stop()
            assert client._task is None

    async def test_connection_error_retries_without_killing_run_loop(self) -> None:
        client = MqttDeviceClient(host="localhost", port=1883)

        class FakeMqttError(Exception):
            pass

        async def end_retry(awaitable: Any, *, timeout: float) -> None:
            assert timeout == 5.0
            awaitable.close()
            client._stop.set()
            raise TimeoutError

        with (
            patch("app.devices.mqtt_client.aiomqtt") as mock_aiomqtt,
            patch("app.devices.mqtt_client.asyncio.wait_for", side_effect=end_retry),
        ):
            mock_aiomqtt.MqttError = FakeMqttError
            mock_aiomqtt.Client.side_effect = FakeMqttError("offline")
            await client._run()

        mock_aiomqtt.Client.assert_called_once()

    async def test_handle_message_parses_and_pushes(self) -> None:
        buf = MqttTelemetryBuffer()
        signals: list[Any] = []

        async def on_signal(sig: Any) -> None:
            signals.append(sig)

        client = MqttDeviceClient(
            host="localhost",
            telemetry_buffer=buf,
            on_signal=on_signal,
        )
        message = MagicMock()
        message.topic = "hub/devices/esp32_001/telemetry"
        message.payload = (
            b'{"sensor_type": "temp", "value": 26.5, "timestamp": "2026-08-17T10:00:00+00:00"}'
        )

        await client._handle(message)
        latest = await buf.latest("esp32_001", "temp")
        assert latest is not None
        assert latest["value"] == 26.5
        assert len(signals) == 1
        assert signals[0].channel == "temp"

    async def test_handle_malformed_json_logs_and_continues(self) -> None:
        buf = MqttTelemetryBuffer()
        client = MqttDeviceClient(host="localhost", telemetry_buffer=buf)
        message = MagicMock()
        message.topic = "hub/devices/esp32_001/telemetry"
        message.payload = b"not-json"

        await client._handle(message)
        assert await buf.latest("esp32_001", "unknown") is None

    async def test_handle_missing_timestamp_uses_now(self) -> None:
        buf = MqttTelemetryBuffer()
        client = MqttDeviceClient(host="localhost", telemetry_buffer=buf)
        message = MagicMock()
        message.topic = "hub/devices/esp32_001/telemetry"
        message.payload = b'{"sensor_type": "motion", "value": 1}'

        await client._handle(message)
        latest = await buf.latest("esp32_001", "motion")
        assert latest is not None
        assert latest["value"] == 1


class TestMqttPresenceBridge:
    async def test_mqtt_message_reaches_presence_perception_end_to_end(
        self, database: Database, user_id: UUID
    ) -> None:
        pipeline = FakePresencePipeline()
        bridge = MqttPresenceBridge(database, pipeline, stable_seconds=0)
        telemetry = MqttTelemetryBuffer()
        client = MqttDeviceClient(
            telemetry_buffer=telemetry,
            on_signal=bridge.handle,
        )
        message = MagicMock()
        message.topic = "hub/devices/ld2410_bedroom/telemetry"
        message.payload = b'{"sensor_type":"presence","value":false}'
        await client._handle(message)
        message.payload = b'{"sensor_type":"presence","value":true}'
        await client._handle(message)
        await bridge.wait_pending()

        latest = await telemetry.latest("ld2410_bedroom", "presence")
        assert latest is not None and latest["value"] is True
        assert len(pipeline.events) == 1
        assert pipeline.events[0].user_id == user_id
        assert pipeline.events[0].attributes["device_id"] == "ld2410_bedroom"

    async def test_stable_transition_enters_perception_and_delivery(
        self, database: Database, user_id: UUID
    ) -> None:
        pipeline = FakePresencePipeline()
        delivered: list[dict[str, Any]] = []

        async def deliver(message: str, **kwargs: Any) -> None:
            delivered.append({"message": message, **kwargs})

        bridge = MqttPresenceBridge(
            database,
            pipeline,
            proactive_deliver=deliver,
            stable_seconds=0,
        )
        await bridge.handle(presence_signal(False))
        await bridge.handle(presence_signal(True))
        await bridge.wait_pending()

        assert len(pipeline.events) == 1
        event = pipeline.events[0]
        assert event.user_id == user_id
        assert event.kind == "presence.changed"
        assert event.source_kind == "mqtt_presence"
        assert event.privacy_level == PrivacyLevel.L1
        assert event.attributes["presence"] == "present"
        assert len(delivered) == 1
        assert delivered[0]["target_user_id"] == user_id

    async def test_bounce_back_to_confirmed_state_cancels_pending_transition(
        self, database: Database
    ) -> None:
        pipeline = FakePresencePipeline()
        release = asyncio.Event()

        async def wait_for_release(_: float) -> None:
            await release.wait()

        bridge = MqttPresenceBridge(
            database,
            pipeline,
            stable_seconds=5,
            sleeper=wait_for_release,
        )
        await bridge.handle(presence_signal(False))
        await bridge.handle(presence_signal(True))
        await bridge.handle(presence_signal(False))
        release.set()
        await bridge.wait_pending()

        assert pipeline.events == []
