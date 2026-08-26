from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.devices.mqtt_client import MqttDeviceClient, MqttTelemetryBuffer, MqttTelemetryMessage
from app.schemas import PrivacyLevel
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


class TestMqttTelemetryBuffer:
    async def test_push_and_latest(self, buffer: MqttTelemetryBuffer, sample_msg: MqttTelemetryMessage) -> None:
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
        assert signals[0].privacy_level == PrivacyLevel.L2

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
        message.payload = b'{"sensor_type": "temp", "value": 26.5, "timestamp": "2026-08-17T10:00:00+00:00"}'

        await client._handle(message)  # type: ignore[arg-type]
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

        await client._handle(message)  # type: ignore[arg-type]
        assert await buf.latest("esp32_001", "unknown") is None

    async def test_handle_missing_timestamp_uses_now(self) -> None:
        buf = MqttTelemetryBuffer()
        client = MqttDeviceClient(host="localhost", telemetry_buffer=buf)
        message = MagicMock()
        message.topic = "hub/devices/esp32_001/telemetry"
        message.payload = b'{"sensor_type": "motion", "value": 1}'

        await client._handle(message)  # type: ignore[arg-type]
        latest = await buf.latest("esp32_001", "motion")
        assert latest is not None
        assert latest["value"] == 1
