from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.schemas import (
    AdapterHealth,
    AdapterManifest,
    AdapterState,
    DeliveryPlan,
    DeliveryReceipt,
    EndpointCapabilities,
    EphemeralSignal,
    InputEnvelope,
)
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
        self._durable = durable or []
        self._ephemeral = ephemeral or []
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

