from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.devices.mqtt_client import MqttTelemetryBuffer
from app.home_assistant import HomeAssistantError
from app.home_assistant.tools import HomeStateProvider
from app.llm import ToolDefinition
from app.schemas import PrivacyLevel
from app.tools.contracts import ToolContext, ToolHandler, ToolResult


class ReadSensorsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    queries: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="查询列表：HA 实体名称/别名、MQTT device_id:sensor_type 或 'all'",
    )


@dataclass(frozen=True, slots=True)
class SensorReading:
    source: str  # "home_assistant" | "mqtt"
    name: str
    value: Any
    unit: str | None = None
    last_updated: str | None = None
    available: bool = True
    reason_code: str | None = None


class ReadSensorsTool(ToolHandler):
    """统一读取 Home Assistant 实体和 MQTT 设备遥测状态。"""

    name = "read_sensors"
    description = (
        "读取已授权的 Home Assistant 实体或 MQTT 设备传感器的当前状态。"
        "支持查询温度、湿度、存在、门窗、光照等传感器。"
        "queries 可以是实体名称、别名、entity_id，或 'all' 读取所有可用传感器。"
    )
    arguments_model: type[BaseModel] = ReadSensorsArgs
    runs_local = True
    max_privacy_level = PrivacyLevel.L2

    def __init__(
        self,
        ha_provider: HomeStateProvider | None = None,
        mqtt_buffer: MqttTelemetryBuffer | None = None,
    ) -> None:
        self._ha = ha_provider
        self._mqtt = mqtt_buffer

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=ReadSensorsArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = ReadSensorsArgs.model_validate(arguments)
        queries = args.queries if args.queries else ["all"]

        if self._ha is None and self._mqtt is None:
            return ToolResult(
                ok=False,
                tool_name=self.name,
                reason_code="no_sensor_providers",
                data={"errors": ["no_sensor_providers"]},
                latency_ms=(perf_counter() - started) * 1_000,
            )

        readings: list[dict[str, Any]] = []
        errors: list[str] = []

        for query in queries:
            if query == "all":
                if self._ha is not None:
                    readings.extend(await self._query_all_ha())
                if self._mqtt is not None:
                    readings.extend(await self._query_all_mqtt())
            elif ":" in query and self._mqtt is not None:
                reading = await self._query_mqtt(query)
                if reading is not None:
                    readings.append(reading)
            elif self._ha is not None:
                reading = await self._query_ha(query)
                if reading is not None:
                    readings.append(reading)
            else:
                errors.append(f"unsupported_query:{query}")

        if not readings and errors:
            return ToolResult(
                ok=False,
                tool_name=self.name,
                reason_code=errors[0],
                data={"errors": errors},
                latency_ms=(perf_counter() - started) * 1_000,
            )

        return ToolResult(
            ok=True,
            tool_name=self.name,
            provider="hub",
            data={
                "readings": readings,
                "count": len(readings),
                "errors": errors if errors else None,
            },
            latency_ms=(perf_counter() - started) * 1_000,
        )

    async def _query_ha(self, target: str) -> dict[str, Any] | None:
        if self._ha is None:
            return None
        try:
            policy = self._ha.resolve(target)
            state = self._ha.get_state(policy.entity_id)
            return {
                "source": "home_assistant",
                "name": policy.display_name,
                "entity_id": policy.entity_id,
                "state": state.state,
                "attributes": {
                    k: v for k, v in (state.attributes or {}).items()
                    if k in set(policy.allowed_attributes)
                },
                "last_updated": state.last_updated.isoformat() if state.last_updated else None,
            }
        except HomeAssistantError as error:
            return {
                "source": "home_assistant",
                "name": target,
                "available": False,
                "reason_code": error.reason_code,
            }

    async def _query_all_ha(self) -> list[dict[str, Any]]:
        # HA 没有 list-all 接口，暂不支持 "all" 模式的 HA 查询
        return []

    async def _query_mqtt(self, query: str) -> dict[str, Any] | None:
        if self._mqtt is None:
            return None
        parts = query.split(":", 1)
        if len(parts) != 2:
            return None
        device_id, sensor_type = parts
        latest = await self._mqtt.latest(device_id, sensor_type)
        if latest is None:
            return {
                "source": "mqtt",
                "name": query,
                "available": False,
                "reason_code": "mqtt_no_data",
            }
        fresh = await self._mqtt.is_fresh(device_id, sensor_type, max_age_seconds=300)
        return {
            "source": "mqtt",
            "name": query,
            "value": latest["value"],
            "last_updated": latest["timestamp"],
            "fresh": fresh,
        }

    async def _query_all_mqtt(self) -> list[dict[str, Any]]:
        if self._mqtt is None:
            return []
        all_latest = await self._mqtt.latest_all()
        return [
            {
                "source": "mqtt",
                "name": key,
                "value": data["value"],
                "last_updated": data["timestamp"],
            }
            for key, data in all_latest.items()
        ]
