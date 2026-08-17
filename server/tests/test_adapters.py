from __future__ import annotations

from app.adapters.builtin import MockInputAdapter, MockOutputAdapter, RecordingInputSink
from app.schemas import (
    AdapterManifest,
    DeliveryPlan,
    EndpointCapabilities,
    EphemeralSignal,
    InputEnvelope,
)


async def test_input_adapter_keeps_durable_and_ephemeral_paths_separate(
    manifest: AdapterManifest,
    input_event: InputEnvelope,
    ephemeral_signal: EphemeralSignal,
) -> None:
    adapter = MockInputAdapter(
        manifest,
        durable=[input_event],
        ephemeral=[ephemeral_signal],
    )
    sink = RecordingInputSink()
    await adapter.configure({})
    await adapter.start()
    await adapter.run(sink)
    assert sink.durable == [input_event]
    assert sink.ephemeral == [ephemeral_signal]


async def test_output_adapter_delivery_is_idempotent(
    manifest: AdapterManifest,
    endpoint_capabilities: EndpointCapabilities,
    delivery_plan: DeliveryPlan,
) -> None:
    adapter = MockOutputAdapter(manifest, endpoint_capabilities)
    await adapter.configure({})
    await adapter.start()
    first = await adapter.deliver(delivery_plan)
    second = await adapter.deliver(delivery_plan)
    assert first == second
    assert first.status == "accepted"


async def test_output_adapter_can_cancel_generation(
    manifest: AdapterManifest,
    endpoint_capabilities: EndpointCapabilities,
    delivery_plan: DeliveryPlan,
) -> None:
    adapter = MockOutputAdapter(manifest, endpoint_capabilities)
    await adapter.configure({})
    await adapter.start()
    await adapter.deliver(delivery_plan)
    assert delivery_plan.generation_id is not None
    receipt = await adapter.cancel(str(delivery_plan.generation_id), delivery_plan.endpoint_id)
    assert receipt.status == "cancelled"
    assert receipt.reason_code == "generation_cancelled"
