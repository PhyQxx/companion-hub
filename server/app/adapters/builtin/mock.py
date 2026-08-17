from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from app.schemas import (
    AdapterCapabilities,
    AdapterHealth,
    AdapterManifest,
    AdapterPrivacy,
    AdapterState,
    DeliveryPlan,
    DeliveryReceipt,
    EndpointCapabilities,
    EphemeralSignal,
    InputEnvelope,
)
from app.schemas.adapter import DeliveryCapabilities
from app.schemas.output import DeliveryStatus

UTC = UTC


class RecordingInputSink:
    def __init__(self) -> None:
        self.durable: list[InputEnvelope] = []
        self.ephemeral: list[EphemeralSignal] = []

    async def emit_durable(self, event: InputEnvelope) -> None:
        self.durable.append(event)

    async def emit_ephemeral(self, signal: EphemeralSignal) -> None:
        self.ephemeral.append(signal)


class MockInputAdapter:
    def __init__(
        self,
        manifest: AdapterManifest,
        *,
        durable: list[InputEnvelope] | None = None,
        ephemeral: list[EphemeralSignal] | None = None,
    ) -> None:
        self.manifest = manifest
        self._durable = durable if durable is not None else []
        self._ephemeral = ephemeral if ephemeral is not None else []
        self._state = AdapterState.INSTALLED
        self._paused = False

    async def configure(self, config: dict[str, object]) -> None:
        del config
        self._state = AdapterState.CONFIGURED

    async def start(self) -> None:
        self._state = AdapterState.READY

    async def health(self) -> AdapterHealth:
        return AdapterHealth(state=self._state, checked_at=datetime.now(UTC))

    async def reload(self, config: dict[str, object]) -> None:
        await self.configure(config)
        await self.start()

    async def stop(self) -> None:
        self._state = AdapterState.STOPPED

    async def run(self, sink: RecordingInputSink) -> None:
        if self._state != AdapterState.READY or self._paused:
            return
        for event in self._durable:
            await sink.emit_durable(event)
        for signal in self._ephemeral:
            await sink.emit_ephemeral(signal)

    async def pause(self, reason: str) -> None:
        del reason
        self._paused = True

    async def resume(self) -> None:
        self._paused = False


class MockOutputAdapter:
    def __init__(self, manifest: AdapterManifest, capabilities: EndpointCapabilities) -> None:
        self.manifest = manifest
        self._capabilities = capabilities
        self._state = AdapterState.INSTALLED
        self._receipts: dict[str, DeliveryReceipt] = {}
        self._plans: dict[UUID, DeliveryPlan] = {}

    async def configure(self, config: dict[str, object]) -> None:
        del config
        self._state = AdapterState.CONFIGURED

    async def start(self) -> None:
        self._state = AdapterState.READY

    async def health(self) -> AdapterHealth:
        return AdapterHealth(state=self._state, checked_at=datetime.now(UTC))

    async def reload(self, config: dict[str, object]) -> None:
        await self.configure(config)
        await self.start()

    async def stop(self) -> None:
        self._state = AdapterState.STOPPED

    async def probe(self, endpoint_id: str) -> EndpointCapabilities:
        if endpoint_id != self._capabilities.endpoint_id:
            raise LookupError(endpoint_id)
        return self._capabilities

    async def deliver(self, plan: DeliveryPlan) -> DeliveryReceipt:
        existing = self._receipts.get(plan.idempotency_key)
        if existing is not None:
            return existing
        receipt = DeliveryReceipt(
            delivery_id=plan.delivery_id,
            intent_id=plan.intent_id,
            adapter_instance_id=plan.adapter_instance_id,
            endpoint_id=plan.endpoint_id,
            status=DeliveryStatus.ACCEPTED,
            attempt=plan.attempt,
            generation_id=plan.generation_id,
            occurred_at=datetime.now(UTC),
        )
        self._receipts[plan.idempotency_key] = receipt
        self._plans[plan.delivery_id] = plan
        return receipt

    async def cancel(self, generation_id: str, endpoint_id: str) -> DeliveryReceipt:
        generation_uuid = UUID(generation_id)
        matching = next(
            (
                plan
                for plan in self._plans.values()
                if plan.generation_id == generation_uuid and plan.endpoint_id == endpoint_id
            ),
            None,
        )
        if matching is None:
            raise LookupError((generation_id, endpoint_id))
        receipt = DeliveryReceipt(
            delivery_id=matching.delivery_id,
            intent_id=matching.intent_id,
            adapter_instance_id=matching.adapter_instance_id,
            endpoint_id=matching.endpoint_id,
            status=DeliveryStatus.CANCELLED,
            attempt=matching.attempt,
            generation_id=matching.generation_id,
            occurred_at=datetime.now(UTC),
            reason_code="generation_cancelled",
        )
        self._receipts[matching.idempotency_key] = receipt
        return receipt


def _manifest(
    adapter_id: str,
    direction: str,
    *,
    input_parts: list[str] | None = None,
    output_parts: list[str] | None = None,
    streaming: list[str] | None = None,
    max_input_level: str = "L2",
    backpressure: Literal["block", "drop_oldest", "sample", "aggregate"] = "block",
    queue_limit: int = 100,
) -> AdapterManifest:
    return AdapterManifest(
        adapter_id=adapter_id,
        adapter_version="0.1.0",
        min_hub_version="0.1.0",
        max_tested_hub_version="0.1.x",
        direction=[direction],
        transport_bindings=["local"],
        config_schema_ref=f"aria.adapter.{adapter_id.removeprefix('builtin.')}-config/1",
        capabilities=AdapterCapabilities(
            input_parts=input_parts or [],
            output_parts=output_parts or [],
            streaming=streaming or [],
            delivery=DeliveryCapabilities(
                supports_ack=direction == "output",
                supports_cancel=direction == "output",
            ),
        ),
        privacy=AdapterPrivacy(
            runs_local=True,
            max_input_level=max_input_level,
            max_output_level="L2",
        ),
        input_policy={"backpressure": backpressure, "queue_limit": queue_limit},
        resource_profile="test-small",
    )


class MockTextInputAdapter(MockInputAdapter):
    def __init__(self, durable: list[InputEnvelope] | None = None) -> None:
        super().__init__(
            _manifest("builtin.mock_text_input", "input", input_parts=["text"]),
            durable=durable,
        )


class MockEphemeralSensorAdapter(MockInputAdapter):
    def __init__(self, ephemeral: list[EphemeralSignal] | None = None) -> None:
        super().__init__(
            _manifest(
                "builtin.mock_ephemeral_sensor",
                "input",
                input_parts=["telemetry"],
                max_input_level="L3",
                backpressure="drop_oldest",
                queue_limit=32,
            ),
            ephemeral=ephemeral,
        )


class MockTextOutputAdapter(MockOutputAdapter):
    def __init__(self) -> None:
        capabilities = EndpointCapabilities(
            endpoint_id="mock-text",
            capabilities=AdapterCapabilities(
                output_parts=["text"],
                delivery=DeliveryCapabilities(supports_ack=True, supports_cancel=True),
            ),
            observed_at=datetime.now(UTC),
        )
        super().__init__(
            _manifest("builtin.mock_text_output", "output", output_parts=["text"]),
            capabilities,
        )


class MockStreamingOutputAdapter(MockOutputAdapter):
    def __init__(self) -> None:
        capabilities = EndpointCapabilities(
            endpoint_id="mock-streaming",
            capabilities=AdapterCapabilities(
                output_parts=["text", "speech"],
                streaming=["text"],
                delivery=DeliveryCapabilities(supports_ack=True, supports_cancel=True),
            ),
            observed_at=datetime.now(UTC),
        )
        super().__init__(
            _manifest(
                "builtin.mock_streaming_output",
                "output",
                output_parts=["text", "speech"],
                streaming=["text"],
            ),
            capabilities,
        )
