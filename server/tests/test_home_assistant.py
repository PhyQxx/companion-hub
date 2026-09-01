from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from pydantic import ValidationError

from app.config import (
    ConfigStore,
    HomeAssistantConfig,
    HomeAssistantEntityConfig,
    HomeAssistantProactiveRuleConfig,
)
from app.home_assistant import (
    HomeAssistantBridge,
    HomeAssistantClient,
    HomeAssistantError,
    HomeAssistantLogEntry,
    HomeAssistantManager,
    HomeAssistantProactiveEngine,
    HomeAssistantState,
    HomeAssistantStateChange,
    HomeControlArgs,
    HomeControlTool,
    HomeGetHistoryArgs,
    HomeGetHistoryTool,
    HomeGetStateArgs,
    HomeGetStateTool,
)
from app.home_assistant.manager import home_assistant_service_for_action
from app.tools import ToolContext
from app.tools.intent import select_device_tools


def _entity(
    entity_id: str = "light.living_room",
    *,
    name: str = "客厅主灯",
    privacy_level: str = "L1",
    read_allowed: bool = True,
    history_allowed: bool = False,
    allowed_actions: list[str] | None = None,
    confirmation_required_actions: list[str] | None = None,
) -> HomeAssistantEntityConfig:
    return HomeAssistantEntityConfig.model_validate(
        {
            "entity_id": entity_id,
            "display_name": name,
            "aliases": ["主灯"] if entity_id == "light.living_room" else [],
            "read_allowed": read_allowed,
            "history_allowed": history_allowed,
            "history_max_hours": 24,
            "allowed_actions": allowed_actions or [],
            "confirmation_required_actions": confirmation_required_actions or [],
            "privacy_level": privacy_level,
            "allowed_attributes": ["friendly_name", "brightness"],
        }
    )


def _config(*entities: HomeAssistantEntityConfig) -> HomeAssistantConfig:
    return HomeAssistantConfig(
        enabled=True,
        base_url="https://ha.example.test:8123",
        secret_ref="env:ARIA_HA_TOKEN",
        state_cache_ttl_seconds=300,
        entities=list(entities),
    )


class FakeGateway:
    def __init__(
        self,
        states: tuple[HomeAssistantState, ...],
        *,
        service_states: tuple[HomeAssistantState, ...] = (),
    ) -> None:
        self.states = states
        self.service_states = service_states
        self.closed = False

    async def fetch_states(self) -> tuple[HomeAssistantState, ...]:
        return self.states

    async def state_changes(self) -> AsyncIterator[HomeAssistantStateChange]:
        if False:
            yield HomeAssistantStateChange("unused.entity", None)

    async def close(self) -> None:
        self.closed = True

    async def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str,
        service_data: dict[str, Any] | None = None,
    ) -> tuple[HomeAssistantState, ...]:
        del domain, service, entity_id, service_data
        return self.service_states

    async def fetch_history(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
    ) -> tuple[HomeAssistantState, ...]:
        del entity_id, start, end
        return self.states

    async def fetch_logbook(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
    ) -> tuple[HomeAssistantLogEntry, ...]:
        del entity_id, start, end
        return ()


class FakeStateProvider:
    def __init__(
        self,
        policy: HomeAssistantEntityConfig,
        state: HomeAssistantState,
    ) -> None:
        self.policy = policy
        self.state = state
        self.control_calls: list[tuple[str, str, dict[str, object] | None]] = []

    def resolve(self, target: str) -> HomeAssistantEntityConfig:
        if target not in {
            self.policy.entity_id,
            self.policy.display_name,
            *self.policy.aliases,
        }:
            raise HomeAssistantError("ha_entity_not_found")
        return self.policy

    def get_state(self, entity_id: str) -> HomeAssistantState:
        if entity_id != self.policy.entity_id:
            raise HomeAssistantError("ha_read_denied")
        return self.state

    async def control(
        self,
        target: str,
        action: str,
        *,
        service_data: dict[str, object] | None = None,
    ) -> HomeAssistantEntityConfig:
        policy = self.resolve(target)
        self.control_calls.append((policy.entity_id, action, service_data))
        return policy

    async def history(
        self,
        target: str,
        start: datetime,
        end: datetime,
    ) -> tuple[HomeAssistantEntityConfig, tuple[HomeAssistantState, ...]]:
        del start, end
        return self.resolve(target), (self.state,)

    async def logbook(
        self,
        target: str,
        start: datetime,
        end: datetime,
    ) -> tuple[HomeAssistantEntityConfig, tuple[HomeAssistantLogEntry, ...]]:
        del start, end
        policy = self.resolve(target)
        return policy, (
            HomeAssistantLogEntry(
                entity_id=policy.entity_id,
                when=self.state.last_updated,
                name=policy.display_name,
                message="turned on",
                domain=policy.entity_id.split(".", 1)[0],
            ),
        )


def test_home_assistant_config_requires_https_and_unique_names() -> None:
    with pytest.raises(ValidationError, match="insecure home assistant HTTP"):
        HomeAssistantConfig(
            enabled=True,
            base_url="http://ha.example.test:8123",
            secret_ref="env:ARIA_HA_TOKEN",
        )

    with pytest.raises(ValidationError, match="display names and aliases must be unique"):
        _config(
            _entity(),
            _entity("sensor.temperature", name="主灯"),
        )

    local = HomeAssistantConfig(
        enabled=True,
        base_url="http://192.168.1.8:8123",
        secret_ref="env:ARIA_HA_TOKEN",
        allow_insecure_local_http=True,
    )
    assert local.base_url is not None and local.base_url.host == "192.168.1.8"

    direct_secret = HomeAssistantConfig(
        enabled=True,
        base_url="https://ha.example.test",
        secret_value="stored-by-admin",
    )
    assert direct_secret.secret_ref is None
    assert direct_secret.secret_value == "stored-by-admin"


def test_home_assistant_semantic_actions_map_to_bounded_services() -> None:
    assert home_assistant_service_for_action("set_brightness") == "turn_on"
    assert home_assistant_service_for_action("play") == "media_play"
    assert home_assistant_service_for_action("pause") == "media_pause"
    assert home_assistant_service_for_action("volume_set") == "volume_set"
    media = _entity(
        "media_player.living_room",
        name="客厅音箱",
        allowed_actions=["play", "pause", "volume_set"],
    )
    assert media.allowed_actions == ["play", "pause", "volume_set"]
    with pytest.raises(ValidationError, match="not valid for entity domain"):
        _entity(
            "switch.unsafe",
            name="错误开关",
            allowed_actions=["play"],
        )


def test_proactive_rule_matching_and_quiet_hours() -> None:
    state = HomeAssistantState(
        entity_id="sensor.temperature",
        state="31.5",
        attributes={},
        last_changed=None,
        last_updated=None,
    )
    rule = HomeAssistantProactiveRuleConfig(
        rule_id="hot",
        kind="temperature_high",
        enabled=True,
        threshold=30,
    )
    policy = _entity("sensor.temperature", name="室温").model_copy(
        update={"proactive_rules": [rule]}
    )
    validated = HomeAssistantEntityConfig.model_validate(policy.model_dump())
    assert HomeAssistantProactiveEngine._matches(validated.proactive_rules[0], state)
    assert HomeAssistantProactiveEngine._in_quiet_hours(
        datetime(2026, 8, 25, 23, 30).time(), "23:00", "07:00"
    )
    assert not HomeAssistantProactiveEngine._in_quiet_hours(
        datetime(2026, 8, 25, 12, 0).time(), "23:00", "07:00"
    )


async def test_manager_accepts_admin_stored_token_and_reconfigures() -> None:
    captured_tokens: list[str] = []
    current_config = _config(_entity())
    current_config = current_config.model_copy(
        update={"secret_ref": None, "secret_value": "admin-token"}
    )
    store = SimpleNamespace(
        current=SimpleNamespace(
            config=SimpleNamespace(integrations=SimpleNamespace(home_assistant=current_config))
        )
    )

    def factory(config: HomeAssistantConfig, token: str) -> FakeGateway:
        del config
        captured_tokens.append(token)
        return FakeGateway(())

    manager = HomeAssistantManager(cast(ConfigStore, store), gateway_factory=factory)
    await manager.start()
    await manager.reconfigure()
    await manager.stop()

    assert captured_tokens == ["admin-token", "admin-token"]


async def test_client_fetches_states_with_bearer_auth_and_parses_payload() -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/states"
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(
            200,
            json=[
                {
                    "entity_id": "light.living_room",
                    "state": "on",
                    "attributes": {"brightness": 120},
                    "last_changed": "2026-08-24T10:00:00+00:00",
                    "last_updated": "2026-08-24T10:00:01+00:00",
                }
            ],
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(transport),
        base_url="https://ha.example.test:8123",
        headers={"Authorization": "Bearer test-token"},
    )
    client = HomeAssistantClient(
        "https://ha.example.test:8123",
        "test-token",
        http_client=http,
    )
    try:
        states = await client.fetch_states()
    finally:
        await http.aclose()

    assert client.websocket_url == "wss://ha.example.test:8123/api/websocket"
    assert states[0].entity_id == "light.living_room"
    assert states[0].state == "on"
    assert states[0].attributes == {"brightness": 120}


async def test_client_maps_auth_error_without_response_payload() -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, text="token and private details")
        ),
        base_url="https://ha.example.test",
    )
    client = HomeAssistantClient(
        "https://ha.example.test",
        "test-token",
        http_client=http,
    )
    try:
        with pytest.raises(HomeAssistantError) as captured:
            await client.fetch_states()
    finally:
        await http.aclose()

    assert captured.value.reason_code == "ha_auth_failed"
    assert "private details" not in str(captured.value)


async def test_client_calls_service_and_reads_history_and_logbook() -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/services/light/turn_on":
            assert request.method == "POST"
            assert request.read() == b'{"entity_id":"light.living_room"}'
            return httpx.Response(200, json=[])
        if request.url.path.startswith("/api/history/period/"):
            assert request.url.params["filter_entity_id"] == "light.living_room"
            return httpx.Response(
                200,
                json=[[{"state": "on", "last_changed": "2026-08-24T09:00:00Z"}]],
            )
        if request.url.path.startswith("/api/logbook/"):
            assert request.url.params["entity"] == "light.living_room"
            return httpx.Response(
                200,
                json=[
                    {
                        "entity_id": "light.living_room",
                        "when": "2026-08-24T09:00:00Z",
                        "name": "客厅灯",
                        "message": "turned on",
                        "domain": "light",
                    }
                ],
            )
        return httpx.Response(404)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(transport),
        base_url="https://ha.example.test",
    )
    client = HomeAssistantClient("https://ha.example.test", "test-token", http_client=http)
    start = datetime(2026, 8, 24, 8, tzinfo=UTC)
    end = datetime(2026, 8, 24, 10, tzinfo=UTC)
    try:
        changed = await client.call_service("light", "turn_on", "light.living_room")
        history = await client.fetch_history("light.living_room", start, end)
        logbook = await client.fetch_logbook("light.living_room", start, end)
    finally:
        await http.aclose()

    assert changed == ()
    assert history[0].entity_id == "light.living_room"
    assert history[0].state == "on"
    assert logbook[0].message == "turned on"


async def test_bridge_caches_only_explicitly_authorized_entities() -> None:
    now = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    allowed = _entity()
    denied = _entity(
        "sensor.private_temperature",
        name="私密温度",
        read_allowed=False,
    )
    gateway = FakeGateway(
        (
            HomeAssistantState(
                entity_id=allowed.entity_id,
                state="on",
                attributes={"friendly_name": "客厅主灯", "brightness": 100},
                last_changed=now,
                last_updated=now,
            ),
            HomeAssistantState(
                entity_id=denied.entity_id,
                state="31.2",
                attributes={"unit_of_measurement": "°C"},
                last_changed=now,
                last_updated=now,
            ),
            HomeAssistantState(
                entity_id="person.someone_else",
                state="home",
                attributes={"latitude": 1, "longitude": 2},
                last_changed=now,
                last_updated=now,
            ),
        )
    )
    bridge = HomeAssistantBridge(_config(allowed, denied), gateway)

    await bridge.refresh_once()

    assert bridge.health().cached_entities == 1
    assert bridge.resolve("主灯").entity_id == allowed.entity_id
    assert bridge.get_state(allowed.entity_id).state == "on"
    with pytest.raises(HomeAssistantError, match="ha_read_denied"):
        bridge.get_state(denied.entity_id)


async def test_bridge_updates_cache_from_service_response_for_readback() -> None:
    now = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    policy = _entity(allowed_actions=["turn_on", "turn_off"])
    off = HomeAssistantState(policy.entity_id, "off", {}, now, now)
    on = HomeAssistantState(policy.entity_id, "on", {}, now, now)
    bridge = HomeAssistantBridge(
        _config(policy),
        FakeGateway((off,), service_states=(on,)),
    )
    await bridge.refresh_once()

    await bridge.call_service(policy.entity_id, "turn_on")

    assert bridge.get_state(policy.entity_id).state == "on"


async def test_bridge_keeps_unchanged_state_valid_while_websocket_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr("app.home_assistant.bridge.monotonic", lambda: clock[0])
    now = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    policy = _entity()
    bridge = HomeAssistantBridge(
        _config(policy).model_copy(update={"state_cache_ttl_seconds": 5}),
        FakeGateway(
            (
                HomeAssistantState(
                    entity_id=policy.entity_id,
                    state="off",
                    attributes={},
                    last_changed=now,
                    last_updated=now,
                ),
            )
        ),
    )

    await bridge.refresh_once()
    clock[0] = 60.0

    assert bridge.get_state(policy.entity_id).state == "off"


async def test_home_get_state_filters_attributes_and_enforces_privacy() -> None:
    now = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    policy = _entity()
    provider = FakeStateProvider(
        policy,
        HomeAssistantState(
            entity_id=policy.entity_id,
            state="on",
            attributes={
                "friendly_name": "客厅主灯",
                "brightness": 120,
                "latitude": 36.6,
            },
            last_changed=now,
            last_updated=now,
        ),
    )
    tool = HomeGetStateTool(provider)
    result = await tool.execute(
        HomeGetStateArgs(targets=["主灯"]),
        ToolContext(privacy_level="L1"),
    )

    assert result.ok is True
    entity = result.data["entities"]
    assert isinstance(entity, list)
    assert entity[0]["attributes"] == {
        "friendly_name": "客厅主灯",
        "brightness": 120,
    }

    l2_policy = _entity(privacy_level="L2")
    l2_tool = HomeGetStateTool(FakeStateProvider(l2_policy, provider.state))
    blocked = await l2_tool.execute(
        HomeGetStateArgs(targets=["主灯"]),
        ToolContext(privacy_level="L1"),
    )
    assert blocked.reason_code == "ha_privacy_level_required"

    l3_policy = _entity(privacy_level="L3")
    l3_tool = HomeGetStateTool(FakeStateProvider(l3_policy, provider.state))
    l3 = await l3_tool.execute(
        HomeGetStateArgs(targets=["主灯"]),
        ToolContext(privacy_level="L3"),
    )
    assert l3.reason_code == "ha_l3_model_access_denied"


async def test_home_control_enforces_policy_and_server_side_confirmation() -> None:
    now = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    light_policy = _entity(
        allowed_actions=["turn_on", "turn_off"],
    )
    light_provider = FakeStateProvider(
        light_policy,
        HomeAssistantState(light_policy.entity_id, "off", {}, now, now),
    )
    light_result = await HomeControlTool(light_provider).execute(
        HomeControlArgs(target="主灯", action="turn_on"),
        ToolContext(privacy_level="L1", user_text="打开客厅主灯"),
    )
    assert light_result.ok is True
    assert light_provider.control_calls == [(light_policy.entity_id, "turn_on", None)]

    climate_policy = _entity(
        "climate.bedroom",
        name="卧室空调",
        allowed_actions=["set_temperature"],
        confirmation_required_actions=["set_temperature"],
    )
    climate_provider = FakeStateProvider(
        climate_policy,
        HomeAssistantState(climate_policy.entity_id, "cool", {}, now, now),
    )
    tool = HomeControlTool(climate_provider)
    blocked = await tool.execute(
        HomeControlArgs(target="卧室空调", action="set_temperature", temperature_c=24),
        ToolContext(privacy_level="L1", user_text="空调调到24度"),
    )
    negated = await tool.execute(
        HomeControlArgs(target="卧室空调", action="set_temperature", temperature_c=24),
        ToolContext(privacy_level="L1", user_text="我还不确定"),
    )
    confirmed = await tool.execute(
        HomeControlArgs(target="卧室空调", action="set_temperature", temperature_c=24),
        ToolContext(privacy_level="L1", user_text="确认调到24度"),
    )
    assert blocked.reason_code == "ha_confirmation_required"
    assert negated.reason_code == "ha_confirmation_required"
    assert climate_provider.control_calls == [
        (climate_policy.entity_id, "set_temperature", {"temperature": 24.0})
    ]
    assert confirmed.ok is True


async def test_home_control_maps_brightness_and_media_parameters() -> None:
    now = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    light_policy = _entity(allowed_actions=["set_brightness"])
    light_provider = FakeStateProvider(
        light_policy,
        HomeAssistantState(light_policy.entity_id, "on", {"brightness": 102}, now, now),
    )
    brightness = await HomeControlTool(light_provider).execute(
        HomeControlArgs(
            target="主灯",
            action="set_brightness",
            brightness_pct=40,
        ),
        ToolContext(privacy_level="L1", user_text="把灯调到40%"),
    )

    media_policy = _entity(
        "media_player.living_room",
        name="客厅音箱",
        allowed_actions=["play", "pause", "volume_set"],
        confirmation_required_actions=["volume_set"],
    )
    media_provider = FakeStateProvider(
        media_policy,
        HomeAssistantState(
            media_policy.entity_id,
            "playing",
            {"volume_level": 0.35},
            now,
            now,
        ),
    )
    media_tool = HomeControlTool(media_provider)
    paused = await media_tool.execute(
        HomeControlArgs(target="客厅音箱", action="pause"),
        ToolContext(privacy_level="L1", user_text="暂停播放"),
    )
    blocked_volume = await media_tool.execute(
        HomeControlArgs(target="客厅音箱", action="volume_set", volume_level=0.35),
        ToolContext(privacy_level="L1", user_text="音量调小"),
    )
    confirmed_volume = await media_tool.execute(
        HomeControlArgs(target="客厅音箱", action="volume_set", volume_level=0.35),
        ToolContext(privacy_level="L1", user_text="确认把音量调到35%"),
    )

    assert brightness.ok is True
    assert light_provider.control_calls == [
        (light_policy.entity_id, "set_brightness", {"brightness_pct": 40})
    ]
    assert paused.ok is True
    assert blocked_volume.reason_code == "ha_confirmation_required"
    assert confirmed_volume.ok is True
    assert media_provider.control_calls == [
        (media_policy.entity_id, "pause", None),
        (media_policy.entity_id, "volume_set", {"volume_level": 0.35}),
    ]


async def test_home_history_returns_bounded_state_and_logbook_rows() -> None:
    now = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    policy = _entity(history_allowed=True)
    provider = FakeStateProvider(
        policy,
        HomeAssistantState(policy.entity_id, "on", {}, now, now),
    )
    result = await HomeGetHistoryTool(provider).execute(
        HomeGetHistoryArgs(target="主灯", hours=72, limit=10),
        ToolContext(privacy_level="L1", user_text="查看客厅灯日志"),
    )

    assert result.ok is True
    assert result.data["hours"] == 24
    assert result.data["states"] == [{"state": "on", "changed_at": now.isoformat()}]
    assert result.data["logbook"][0]["message"] == "turned on"


def test_home_tool_is_selected_only_with_live_capability_and_intent() -> None:
    capability = "home_assistant:light.living_room:state.read"

    assert select_device_tools("客厅灯开着吗", [capability]) == ("home_get_state",)
    assert select_device_tools("随便聊聊", [capability]) == ()
    assert select_device_tools("客厅灯开着吗", []) == ()

    assert select_device_tools(
        "客厅灯的开关记录",
        ["home_assistant:light.living_room:history.read"],
    ) == ("home_get_history",)
    assert select_device_tools(
        "打开客厅灯",
        ["home_assistant:light.living_room:turn_on"],
    ) == ("home_control",)
    assert select_device_tools(
        "确认",
        ["home_assistant:climate.bedroom:set_temperature"],
    ) == ("home_control",)
