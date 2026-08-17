from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.adapters import OutputAdapter
from app.ids import uuid7
from app.schemas import (
    AdapterCapabilities,
    AdapterManifest,
    AdapterState,
    DeliveryPlan,
    DeliveryReceipt,
    EndpointCapabilities,
    OutputIntent,
    PrivacyLevel,
)
from app.schemas.adapter import DeliveryCapabilities, PresentationCapabilities
from app.schemas.output import DeliveryStatus, Presentation

_PRIVACY_RANK = {PrivacyLevel.L0: 0, PrivacyLevel.L1: 1, PrivacyLevel.L2: 2, PrivacyLevel.L3: 3}


class NoCompatibleOutput(LookupError):
    def __init__(self, reason_code: str = "no_compatible_output") -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _intersection(*values: list[str]) -> list[str]:
    if not values:
        return []
    allowed = set(values[0])
    for current in values[1:]:
        allowed.intersection_update(current)
    return [value for value in values[0] if value in allowed]


def intersect_capabilities(
    declared: AdapterCapabilities,
    authorized: AdapterCapabilities,
    observed: AdapterCapabilities,
) -> AdapterCapabilities:
    return AdapterCapabilities(
        input_parts=_intersection(
            declared.input_parts, authorized.input_parts, observed.input_parts
        ),
        output_parts=_intersection(
            declared.output_parts, authorized.output_parts, observed.output_parts
        ),
        streaming=_intersection(declared.streaming, authorized.streaming, observed.streaming),
        presentation=PresentationCapabilities(
            engine=(
                declared.presentation.engine
                if declared.presentation.engine
                == authorized.presentation.engine
                == observed.presentation.engine
                else None
            ),
            emotions=_intersection(
                declared.presentation.emotions,
                authorized.presentation.emotions,
                observed.presentation.emotions,
            ),
            gestures=_intersection(
                declared.presentation.gestures,
                authorized.presentation.gestures,
                observed.presentation.gestures,
            ),
        ),
        delivery=DeliveryCapabilities(
            supports_ack=(
                declared.delivery.supports_ack
                and authorized.delivery.supports_ack
                and observed.delivery.supports_ack
            ),
            supports_cancel=(
                declared.delivery.supports_cancel
                and authorized.delivery.supports_cancel
                and observed.delivery.supports_cancel
            ),
            supports_replace=(
                declared.delivery.supports_replace
                and authorized.delivery.supports_replace
                and observed.delivery.supports_replace
            ),
            proactive_reachable=(
                declared.delivery.proactive_reachable
                and authorized.delivery.proactive_reachable
                and observed.delivery.proactive_reachable
            ),
        ),
        critical=declared.critical,
    )


@dataclass(frozen=True, slots=True)
class EndpointRegistration:
    adapter_instance_id: UUID
    endpoint_id: str
    adapter: OutputAdapter
    manifest: AdapterManifest
    authorized: AdapterCapabilities
    max_privacy_level: PrivacyLevel | None = None
    public: bool = False


@dataclass(frozen=True, slots=True)
class _Candidate:
    registration: EndpointRegistration
    capabilities: EndpointCapabilities
    selected_content: list[dict[str, object]]
    presentation: Presentation


class OutputRouter:
    def __init__(self) -> None:
        self._endpoints: dict[str, EndpointRegistration] = {}
        self._plans: dict[tuple[UUID, str], DeliveryPlan] = {}
        self._cancelled_generations: set[UUID] = set()

    def register(self, endpoint: EndpointRegistration) -> None:
        if endpoint.endpoint_id in self._endpoints:
            raise ValueError(f"endpoint already registered: {endpoint.endpoint_id}")
        if "output" not in endpoint.manifest.direction:
            raise ValueError("input-only adapter cannot register an output endpoint")
        self._endpoints[endpoint.endpoint_id] = endpoint

    async def route(self, intent: OutputIntent) -> list[DeliveryReceipt]:
        if intent.generation_id in self._cancelled_generations:
            raise NoCompatibleOutput("generation_cancelled")
        candidates = await self._candidates(intent)
        if not candidates:
            raise NoCompatibleOutput()
        selected = candidates if intent.target_selector.mode == "all_compatible" else candidates[:1]
        receipts: list[DeliveryReceipt] = []
        for candidate in selected:
            plan = self._plan(intent, candidate)
            remaining = (plan.deadline_at - datetime.now(UTC)).total_seconds()
            configured = intent.delivery.ttl_ms / 1_000
            timeout = max(min(remaining, configured), 0.001)
            try:
                receipt = await asyncio.wait_for(
                    candidate.registration.adapter.deliver(plan), timeout=timeout
                )
            except TimeoutError:
                receipt = DeliveryReceipt(
                    delivery_id=plan.delivery_id,
                    intent_id=plan.intent_id,
                    adapter_instance_id=plan.adapter_instance_id,
                    endpoint_id=plan.endpoint_id,
                    status=DeliveryStatus.UNKNOWN_OUTCOME,
                    attempt=plan.attempt,
                    generation_id=plan.generation_id,
                    occurred_at=datetime.now(UTC),
                    reason_code="delivery_timeout",
                )
            if intent.generation_id in self._cancelled_generations:
                continue
            receipts.append(receipt)
        return receipts

    async def cancel_generation(self, generation_id: UUID) -> None:
        self._cancelled_generations.add(generation_id)
        for endpoint in self._endpoints.values():
            observed = await endpoint.adapter.probe(endpoint.endpoint_id)
            effective = intersect_capabilities(
                endpoint.manifest.capabilities,
                endpoint.authorized,
                observed.capabilities,
            )
            if not effective.delivery.supports_cancel:
                continue
            with suppress(LookupError):
                await endpoint.adapter.cancel(str(generation_id), endpoint.endpoint_id)

    async def _candidates(self, intent: OutputIntent) -> list[_Candidate]:
        candidates: list[_Candidate] = []
        explicit = set(intent.target_selector.endpoint_ids)
        for endpoint in self._ordered_endpoints(intent):
            if intent.target_selector.mode == "explicit" and endpoint.endpoint_id not in explicit:
                continue
            if endpoint.public and PrivacyLevel(intent.privacy_level) in {
                PrivacyLevel.L2,
                PrivacyLevel.L3,
            }:
                continue
            maximum = PrivacyLevel(endpoint.manifest.privacy.max_output_level)
            if endpoint.max_privacy_level is not None:
                authorized_maximum = PrivacyLevel(endpoint.max_privacy_level)
                if _PRIVACY_RANK[authorized_maximum] < _PRIVACY_RANK[maximum]:
                    maximum = authorized_maximum
            if _PRIVACY_RANK[PrivacyLevel(intent.privacy_level)] > _PRIVACY_RANK[maximum]:
                continue
            health = await endpoint.adapter.health()
            if health.state not in {AdapterState.READY, AdapterState.DEGRADED}:
                continue
            observed = await endpoint.adapter.probe(endpoint.endpoint_id)
            effective = intersect_capabilities(
                endpoint.manifest.capabilities, endpoint.authorized, observed.capabilities
            )
            selected = self._select_content(intent, effective)
            if not selected:
                continue
            presentation = self._presentation(intent.presentation, effective)
            candidates.append(_Candidate(endpoint, observed, selected, presentation))
        return candidates

    def _ordered_endpoints(self, intent: OutputIntent) -> list[EndpointRegistration]:
        preferred = {
            endpoint: index for index, endpoint in enumerate(intent.target_selector.prefer)
        }
        return sorted(
            self._endpoints.values(),
            key=lambda endpoint: preferred.get(endpoint.endpoint_id, len(preferred)),
        )

    @staticmethod
    def _select_content(
        intent: OutputIntent, capabilities: AdapterCapabilities
    ) -> list[dict[str, object]]:
        selected: list[dict[str, object]] = []
        supported = set(capabilities.output_parts)
        for part in intent.content:
            if part.type in supported:
                selected.append(part.model_dump(mode="python"))
            elif part.type == "speech" and "text" in supported:
                selected.append(
                    {
                        "type": "text",
                        "text": part.text,
                        "language": part.language,
                        "format": "plain",
                    }
                )
        return selected

    @staticmethod
    def _presentation(
        requested: Presentation, capabilities: AdapterCapabilities
    ) -> Presentation:
        emotion = (
            requested.emotion
            if requested.emotion in capabilities.presentation.emotions
            else None
        )
        gesture = requested.gesture
        if gesture not in capabilities.presentation.gestures:
            gesture = "idle" if "idle" in capabilities.presentation.gestures else None
        return Presentation(emotion=emotion, intensity=requested.intensity, gesture=gesture)

    def _plan(self, intent: OutputIntent, candidate: _Candidate) -> DeliveryPlan:
        key = (intent.intent_id, candidate.registration.endpoint_id)
        existing = self._plans.get(key)
        if existing is not None:
            return existing
        canonical = json.dumps(candidate.selected_content, sort_keys=True, default=str)
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:24]
        plan = DeliveryPlan(
            delivery_id=uuid7(),
            intent_id=intent.intent_id,
            adapter_instance_id=candidate.registration.adapter_instance_id,
            endpoint_id=candidate.registration.endpoint_id,
            generation_id=intent.generation_id,
            privacy_level=intent.privacy_level,
            selected_content=candidate.selected_content,
            presentation=candidate.presentation,
            deadline_at=intent.created_at + timedelta(milliseconds=intent.delivery.ttl_ms),
            idempotency_key=f"{intent.intent_id}:{candidate.registration.endpoint_id}:{digest}",
        )
        self._plans[key] = plan
        return plan
