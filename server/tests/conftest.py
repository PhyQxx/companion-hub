from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.schemas import (
    AdapterCapabilities,
    AdapterManifest,
    AdapterPrivacy,
    DeliveryPlan,
    EndpointCapabilities,
    EphemeralSignal,
    InputEnvelope,
)
from app.schemas.adapter import DeliveryCapabilities, PresentationCapabilities
from app.schemas.common import SourceRef

UUIDS = {
    name: f"0198b2f4-3b00-7{index:03x}-8000-{index:012x}"
    for index, name in enumerate(
        [
            "event",
            "correlation",
            "user",
            "conversation",
            "turn",
            "generation",
            "adapter",
            "signal",
            "intent",
            "delivery",
        ],
        start=1,
    )
}
NOW = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)


@pytest.fixture
def source() -> SourceRef:
    return SourceRef(
        adapter_id="builtin.mock_input",
        adapter_instance_id=UUIDS["adapter"],
        endpoint_id="test-endpoint",
    )


@pytest.fixture
def input_event(source: SourceRef) -> InputEnvelope:
    return InputEnvelope(
        event_id=UUIDS["event"],
        correlation_id=UUIDS["correlation"],
        user_id=UUIDS["user"],
        conversation_id=UUIDS["conversation"],
        turn_id=UUIDS["turn"],
        source=source,
        kind="message.received",
        occurred_at=NOW,
        received_at=NOW + timedelta(milliseconds=10),
        privacy_level="L1",
        content=[{"type": "text", "text": "hello", "language": "en"}],
    )


@pytest.fixture
def ephemeral_signal(source: SourceRef) -> EphemeralSignal:
    return EphemeralSignal(
        signal_id=UUIDS["signal"],
        source=source,
        channel="pressure",
        occurred_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
        privacy_level="L3",
        content={"type": "telemetry", "channel": "pressure", "value": 0.73},
    )


@pytest.fixture
def capabilities() -> AdapterCapabilities:
    return AdapterCapabilities(
        input_parts=["text", "telemetry"],
        output_parts=["text", "speech"],
        streaming=["text"],
        presentation=PresentationCapabilities(emotions=["neutral", "tender"]),
        delivery=DeliveryCapabilities(supports_ack=True, supports_cancel=True),
    )


@pytest.fixture
def manifest(capabilities: AdapterCapabilities) -> AdapterManifest:
    return AdapterManifest(
        adapter_id="builtin.mock_adapter",
        adapter_version="0.1.0",
        min_hub_version="0.1.0",
        max_tested_hub_version="0.1.x",
        direction=["input", "output"],
        transport_bindings=["local"],
        config_schema_ref="aria.adapter.mock-config/1",
        capabilities=capabilities,
        privacy=AdapterPrivacy(
            runs_local=True,
            max_input_level="L3",
            max_output_level="L2",
        ),
        permissions=["local_audio"],
        resource_profile="realtime-small",
    )


@pytest.fixture
def endpoint_capabilities(capabilities: AdapterCapabilities) -> EndpointCapabilities:
    return EndpointCapabilities(
        endpoint_id="test-endpoint",
        capabilities=capabilities,
        observed_at=NOW,
    )


@pytest.fixture
def delivery_plan() -> DeliveryPlan:
    return DeliveryPlan(
        delivery_id=UUIDS["delivery"],
        intent_id=UUIDS["intent"],
        adapter_instance_id=UUIDS["adapter"],
        endpoint_id="test-endpoint",
        generation_id=UUIDS["generation"],
        privacy_level="L1",
        selected_content=[{"type": "text", "text": "hello"}],
        deadline_at=NOW + timedelta(seconds=8),
        idempotency_key="intent:endpoint:hash",
    )

