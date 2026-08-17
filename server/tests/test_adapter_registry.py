from __future__ import annotations

from uuid import UUID

import pytest

from app.adapters import (
    Adapter,
    AdapterCompatibilityError,
    AdapterLifecycleManager,
    AdapterRegistrationError,
    AdapterRegistry,
    RuntimeState,
)
from app.adapters.builtin import (
    MockEphemeralSensorAdapter,
    MockInputAdapter,
    MockStreamingOutputAdapter,
    MockTextInputAdapter,
    MockTextOutputAdapter,
)
from app.schemas import AdapterManifest

INSTANCE_ID = UUID("0198b2f4-3b00-7001-8000-000000000001")


def test_registry_only_creates_explicitly_registered_adapters() -> None:
    registry = AdapterRegistry(hub_version="0.1.0")
    registry.register(MockTextInputAdapter)
    assert registry.create("builtin.mock_text_input").manifest.adapter_id == (
        "builtin.mock_text_input"
    )
    with pytest.raises(LookupError, match="not registered"):
        registry.create("external.from_config_path")
    with pytest.raises(AdapterRegistrationError, match="already registered"):
        registry.register(MockTextInputAdapter)


def test_unknown_optional_capabilities_are_kept_but_unknown_critical_are_rejected() -> None:
    base = MockTextInputAdapter().manifest
    optional_manifest = AdapterManifest.model_validate(
        {
            **base.model_dump(mode="python"),
            "adapter_id": "builtin.optional_capability",
            "capabilities": {
                **base.capabilities.model_dump(mode="python"),
                "streaming": ["future_optional_stream"],
            },
        }
    )
    critical_manifest = AdapterManifest.model_validate(
        {
            **base.model_dump(mode="python"),
            "adapter_id": "builtin.critical_capability",
            "capabilities": {
                **base.capabilities.model_dump(mode="python"),
                "critical": ["future_critical"],
            },
        }
    )
    registry = AdapterRegistry(hub_version="0.1.0", known_capabilities={"text"})
    registry.register(lambda: MockInputAdapter(optional_manifest))
    registry.register(lambda: MockInputAdapter(critical_manifest))
    assert registry.create("builtin.optional_capability").manifest == optional_manifest
    with pytest.raises(AdapterCompatibilityError) as error:
        registry.create("builtin.critical_capability")
    assert error.value.reason_code == "upgrade_required"


def test_adapter_requiring_newer_hub_is_rejected() -> None:
    base = MockTextInputAdapter().manifest
    manifest = AdapterManifest.model_validate(
        {**base.model_dump(mode="python"), "min_hub_version": "0.2.0"}
    )
    registry = AdapterRegistry(hub_version="0.1.0")
    registry.register(lambda: MockInputAdapter(manifest))
    with pytest.raises(AdapterCompatibilityError) as error:
        registry.create(manifest.adapter_id)
    assert error.value.reason_code == "hub_upgrade_required"


async def test_failed_reload_restores_last_usable_configuration() -> None:
    class RejectingReloadAdapter(MockTextInputAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.applied: list[dict[str, object]] = []

        async def reload(self, config: dict[str, object]) -> None:
            self.applied.append(config)
            if config.get("reject") is True:
                raise ValueError("synthetic invalid config")
            await super().reload(config)

    registry = AdapterRegistry(hub_version="0.1.0")
    registry.register(RejectingReloadAdapter)
    lifecycle = AdapterLifecycleManager(registry)
    instance = await lifecycle.start(INSTANCE_ID, "builtin.mock_text_input", {"version": 1})

    with pytest.raises(ValueError, match="synthetic invalid config"):
        await lifecycle.reload(INSTANCE_ID, {"version": 2, "reject": True})

    assert instance.config == {"version": 1}
    assert instance.state is RuntimeState.READY
    assert instance.reason_code == "reload_rejected_rolled_back"
    assert isinstance(instance.adapter, RejectingReloadAdapter)
    assert instance.adapter.applied[-1] == {"version": 1}


def test_four_m0_reference_adapters_have_distinct_declared_capabilities() -> None:
    adapters: list[Adapter] = [
        MockTextInputAdapter(),
        MockEphemeralSensorAdapter(),
        MockTextOutputAdapter(),
        MockStreamingOutputAdapter(),
    ]
    assert {adapter.manifest.adapter_id for adapter in adapters} == {
        "builtin.mock_text_input",
        "builtin.mock_ephemeral_sensor",
        "builtin.mock_text_output",
        "builtin.mock_streaming_output",
    }
    assert MockEphemeralSensorAdapter().manifest.privacy.max_input_level == "L3"
    assert MockStreamingOutputAdapter().manifest.capabilities.streaming == ["text"]
