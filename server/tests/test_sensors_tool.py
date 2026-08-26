from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.home_assistant import HomeAssistantError
from app.home_assistant.tools import HomeStateProvider
from app.schemas import PrivacyLevel
from app.tools import ToolContext
from app.tools.sensors import ReadSensorsArgs, ReadSensorsTool


class FakeHaProvider(HomeStateProvider):
    def __init__(self) -> None:
        self._states: dict[str, Any] = {
            "light.living_room": SimpleNamespace(
                state="on",
                attributes={"brightness": 255, "friendly_name": "客厅灯"},
                last_updated=datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC),
            ),
        }

    def resolve(self, target: str) -> SimpleNamespace:
        aliases = {"主灯": "light.living_room"}
        entity_id = aliases.get(target, target)
        return SimpleNamespace(
            entity_id=entity_id,
            display_name="客厅灯",
            allowed_attributes=["brightness", "friendly_name"],
        )

    def get_state(self, entity_id: str) -> Any:
        if entity_id not in self._states:
            raise HomeAssistantError(f"entity_not_found:{entity_id}")
        return self._states[entity_id]


class FakeMqttBuffer:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {
            "esp32_001:temp": {
                "value": 26.5,
                "timestamp": "2026-08-17T10:00:00+00:00",
            },
        }

    async def latest(self, device_id: str, sensor_type: str) -> dict[str, Any] | None:
        return self._data.get(f"{device_id}:{sensor_type}")

    async def latest_all(self) -> dict[str, dict[str, Any]]:
        return dict(self._data)

    async def is_fresh(self, device_id: str, sensor_type: str, max_age_seconds: float = 300.0) -> bool:
        return True


@pytest.fixture
def context() -> ToolContext:
    return ToolContext(
        privacy_level=PrivacyLevel.L2,
        user_id=UUID("0198b2f4-3b00-7001-8000-000000000001"),
        turn_id=UUID("0198b2f4-3b00-7002-8000-000000000002"),
    )


class TestReadSensorsTool:
    async def test_query_ha_entity(self, context: ToolContext) -> None:
        tool = ReadSensorsTool(ha_provider=FakeHaProvider())
        result = await tool.execute(
            ReadSensorsArgs(queries=["主灯"]),
            context,
        )
        assert result.ok is True
        assert result.tool_name == "read_sensors"
        readings = result.data["readings"]
        assert len(readings) == 1
        assert readings[0]["source"] == "home_assistant"
        assert readings[0]["name"] == "客厅灯"
        assert readings[0]["state"] == "on"

    async def test_query_mqtt_sensor(self, context: ToolContext) -> None:
        tool = ReadSensorsTool(mqtt_buffer=FakeMqttBuffer())
        result = await tool.execute(
            ReadSensorsArgs(queries=["esp32_001:temp"]),
            context,
        )
        assert result.ok is True
        readings = result.data["readings"]
        assert len(readings) == 1
        assert readings[0]["source"] == "mqtt"
        assert readings[0]["value"] == 26.5
        assert readings[0]["fresh"] is True

    async def test_query_all_mqtt(self, context: ToolContext) -> None:
        tool = ReadSensorsTool(mqtt_buffer=FakeMqttBuffer())
        result = await tool.execute(
            ReadSensorsArgs(queries=["all"]),
            context,
        )
        assert result.ok is True
        readings = result.data["readings"]
        assert len(readings) == 1
        assert readings[0]["source"] == "mqtt"

    async def test_ha_entity_not_found(self, context: ToolContext) -> None:
        tool = ReadSensorsTool(ha_provider=FakeHaProvider())
        result = await tool.execute(
            ReadSensorsArgs(queries=["nonexistent.entity"]),
            context,
        )
        assert result.ok is True  # tool succeeds, individual reading marks unavailable
        readings = result.data["readings"]
        assert len(readings) == 1
        assert readings[0]["available"] is False
        assert "entity_not_found" in readings[0]["reason_code"]

    async def test_mqtt_sensor_missing(self, context: ToolContext) -> None:
        tool = ReadSensorsTool(mqtt_buffer=FakeMqttBuffer())
        result = await tool.execute(
            ReadSensorsArgs(queries=["missing:sensor"]),
            context,
        )
        assert result.ok is True
        readings = result.data["readings"]
        assert len(readings) == 1
        assert readings[0]["available"] is False
        assert readings[0]["reason_code"] == "mqtt_no_data"

    async def test_no_providers_returns_error(self, context: ToolContext) -> None:
        tool = ReadSensorsTool()
        result = await tool.execute(
            ReadSensorsArgs(queries=["all"]),
            context,
        )
        assert result.ok is False
        assert result.reason_code == "no_sensor_providers"

    async def test_mixed_queries(self, context: ToolContext) -> None:
        tool = ReadSensorsTool(
            ha_provider=FakeHaProvider(),
            mqtt_buffer=FakeMqttBuffer(),
        )
        result = await tool.execute(
            ReadSensorsArgs(queries=["主灯", "esp32_001:temp"]),
            context,
        )
        assert result.ok is True
        assert result.data["count"] == 2
        sources = {r["source"] for r in result.data["readings"]}
        assert sources == {"home_assistant", "mqtt"}

    async def test_default_to_all_when_empty(self, context: ToolContext) -> None:
        tool = ReadSensorsTool(mqtt_buffer=FakeMqttBuffer())
        result = await tool.execute(
            ReadSensorsArgs(queries=[]),
            context,
        )
        assert result.ok is True
        assert result.data["count"] == 1

    def test_definition(self) -> None:
        tool = ReadSensorsTool()
        definition = tool.definition()
        assert definition.name == "read_sensors"
        assert "Home Assistant" in definition.description

    def test_args_validation_too_many_queries(self) -> None:
        with pytest.raises(ValidationError):
            ReadSensorsArgs(queries=["q"] * 11)
