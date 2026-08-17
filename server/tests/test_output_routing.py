from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from conftest import NOW, UUIDS

from app.adapters.builtin import MockStreamingOutputAdapter, MockTextOutputAdapter
from app.output import EndpointRegistration, NoCompatibleOutput, OutputRouter
from app.schemas import (
    AdapterCapabilities,
    DeliveryPlan,
    DeliveryReceipt,
    OutputIntent,
    PrivacyLevel,
)
from app.schemas.adapter import DeliveryCapabilities


def make_intent(
    *,
    content: list[dict[str, object]] | None = None,
    privacy_level: str = "L1",
    ttl_ms: int = 8_000,
) -> OutputIntent:
    return OutputIntent(
        intent_id=UUIDS["intent"],
        correlation_id=UUIDS["correlation"],
        user_id=UUIDS["user"],
        conversation_id=UUIDS["conversation"],
        turn_id=UUIDS["turn"],
        generation_id=UUIDS["generation"],
        kind="reply",
        privacy_level=privacy_level,
        content=content or [{"type": "speech", "text": "hello", "language": "en"}],
        delivery={"ttl_ms": ttl_ms},
        created_at=NOW,
    )


async def register_text_endpoint(
    router: OutputRouter,
    adapter: MockTextOutputAdapter,
    *,
    public: bool = False,
    max_privacy_level: PrivacyLevel | None = None,
) -> None:
    await adapter.configure({})
    await adapter.start()
    router.register(
        EndpointRegistration(
            adapter_instance_id=UUID(UUIDS["adapter"]),
            endpoint_id="mock-text",
            adapter=adapter,
            manifest=adapter.manifest,
            authorized=adapter.manifest.capabilities,
            max_privacy_level=max_privacy_level,
            public=public,
        )
    )


async def test_speech_falls_back_to_text_and_retries_are_idempotent() -> None:
    class CapturingAdapter(MockTextOutputAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.deliveries: list[DeliveryPlan] = []

        async def deliver(self, plan: DeliveryPlan) -> DeliveryReceipt:
            self.deliveries.append(plan)
            return await super().deliver(plan)

    adapter = CapturingAdapter()
    router = OutputRouter()
    await register_text_endpoint(router, adapter)
    intent = make_intent()

    first = await router.route(intent)
    second = await router.route(intent)
    third = await router.route(intent)

    assert first == second == third
    assert adapter.deliveries[0].selected_content[0].type == "text"
    assert adapter.deliveries[0].selected_content[0].text == "hello"
    assert len({plan.delivery_id for plan in adapter.deliveries}) == 1


async def test_public_endpoint_rejects_l2_content() -> None:
    router = OutputRouter()
    await register_text_endpoint(router, MockTextOutputAdapter(), public=True)
    with pytest.raises(NoCompatibleOutput) as error:
        await router.route(make_intent(privacy_level="L2"))
    assert error.value.reason_code == "no_compatible_output"


async def test_admin_can_narrow_endpoint_privacy_below_manifest() -> None:
    router = OutputRouter()
    await register_text_endpoint(
        router,
        MockTextOutputAdapter(),
        max_privacy_level=PrivacyLevel.L0,
    )
    with pytest.raises(NoCompatibleOutput):
        await router.route(make_intent(privacy_level="L1"))


async def test_admin_authorization_is_part_of_capability_intersection() -> None:
    adapter = MockStreamingOutputAdapter()
    await adapter.configure({})
    await adapter.start()
    router = OutputRouter()
    router.register(
        EndpointRegistration(
            adapter_instance_id=UUID(UUIDS["adapter"]),
            endpoint_id="mock-streaming",
            adapter=adapter,
            manifest=adapter.manifest,
            authorized=AdapterCapabilities(
                output_parts=["text"],
                delivery=DeliveryCapabilities(supports_ack=True, supports_cancel=True),
            ),
        )
    )
    receipt = await router.route(make_intent())
    assert receipt[0].status == "accepted"


async def test_timeout_becomes_unknown_outcome_without_automatic_retry() -> None:
    class SlowAdapter(MockTextOutputAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def deliver(self, plan: DeliveryPlan) -> DeliveryReceipt:
            del plan
            self.attempts += 1
            await asyncio.sleep(1)
            raise AssertionError("wait_for should cancel the local wait")

    adapter = SlowAdapter()
    router = OutputRouter()
    await register_text_endpoint(router, adapter)
    receipts = await router.route(make_intent(ttl_ms=1))
    assert receipts[0].status == "unknown_outcome"
    assert receipts[0].reason_code == "delivery_timeout"
    assert adapter.attempts == 1


async def test_cancelled_generation_rejects_late_routing() -> None:
    adapter = MockTextOutputAdapter()
    router = OutputRouter()
    await register_text_endpoint(router, adapter)
    intent = make_intent()
    await router.route(intent)
    assert intent.generation_id is not None
    await router.cancel_generation(intent.generation_id)
    with pytest.raises(NoCompatibleOutput) as error:
        await router.route(intent)
    assert error.value.reason_code == "generation_cancelled"


async def test_cancel_during_delivery_suppresses_late_side_effect() -> None:
    class CancellableAdapter(MockTextOutputAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.released = asyncio.Event()
            self.cancelled = False
            self.side_effects = 0
            self.plan: DeliveryPlan | None = None

        async def deliver(self, plan: DeliveryPlan) -> DeliveryReceipt:
            self.plan = plan
            self.started.set()
            await self.released.wait()
            if not self.cancelled:
                self.side_effects += 1
            return DeliveryReceipt(
                delivery_id=plan.delivery_id,
                intent_id=plan.intent_id,
                adapter_instance_id=plan.adapter_instance_id,
                endpoint_id=plan.endpoint_id,
                status="cancelled" if self.cancelled else "accepted",
                attempt=plan.attempt,
                generation_id=plan.generation_id,
                occurred_at=datetime.now(UTC),
                reason_code="generation_cancelled" if self.cancelled else None,
            )

        async def cancel(self, generation_id: str, endpoint_id: str) -> DeliveryReceipt:
            del generation_id, endpoint_id
            assert self.plan is not None
            self.cancelled = True
            self.released.set()
            return DeliveryReceipt(
                delivery_id=self.plan.delivery_id,
                intent_id=self.plan.intent_id,
                adapter_instance_id=self.plan.adapter_instance_id,
                endpoint_id=self.plan.endpoint_id,
                status="cancelled",
                attempt=self.plan.attempt,
                generation_id=self.plan.generation_id,
                occurred_at=datetime.now(UTC),
                reason_code="generation_cancelled",
            )

    adapter = CancellableAdapter()
    router = OutputRouter()
    await register_text_endpoint(router, adapter)
    intent = make_intent()
    routing = asyncio.create_task(router.route(intent))
    await asyncio.wait_for(adapter.started.wait(), timeout=1)
    assert intent.generation_id is not None
    await router.cancel_generation(intent.generation_id)
    assert await routing == []
    assert adapter.side_effects == 0
