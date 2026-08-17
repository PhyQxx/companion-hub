from __future__ import annotations

from typing import Protocol

from app.schemas import (
    AdapterHealth,
    AdapterManifest,
    DeliveryPlan,
    DeliveryReceipt,
    EndpointCapabilities,
    EphemeralSignal,
    InputEnvelope,
)


class Adapter(Protocol):
    manifest: AdapterManifest

    async def configure(self, config: dict[str, object]) -> None: ...

    async def start(self) -> None: ...

    async def health(self) -> AdapterHealth: ...

    async def reload(self, config: dict[str, object]) -> None: ...

    async def stop(self) -> None: ...


class InputSink(Protocol):
    async def emit_durable(self, event: InputEnvelope) -> None: ...

    async def emit_ephemeral(self, signal: EphemeralSignal) -> None: ...


class InputAdapter(Adapter, Protocol):
    async def run(self, sink: InputSink) -> None: ...

    async def pause(self, reason: str) -> None: ...

    async def resume(self) -> None: ...


class OutputAdapter(Adapter, Protocol):
    async def probe(self, endpoint_id: str) -> EndpointCapabilities: ...

    async def deliver(self, plan: DeliveryPlan) -> DeliveryReceipt: ...

    async def cancel(self, generation_id: str, endpoint_id: str) -> DeliveryReceipt: ...

