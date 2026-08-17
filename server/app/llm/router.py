from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from app.observability import TraceRecorder
from app.privacy import EgressBlocked, EgressDestination, EgressGuard
from app.schemas import PrivacyLevel

from .contracts import (
    CompletionRequest,
    CompletionResult,
    LLMRoute,
    ModelEndpoint,
    RoutePolicy,
)
from .provider import LLMProvider


class LLMRouteExhausted(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class LLMRouter:
    def __init__(
        self,
        *,
        endpoints: Mapping[str, ModelEndpoint],
        routes: Mapping[LLMRoute, RoutePolicy],
        providers: Mapping[str, LLMProvider],
        egress: EgressGuard | None = None,
        traces: TraceRecorder | None = None,
    ) -> None:
        self._endpoints = dict(endpoints)
        self._routes = dict(routes)
        self._providers = dict(providers)
        self._egress = egress or EgressGuard()
        self._traces = traces
        self._validate_configuration()

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        privacy = PrivacyLevel(request.privacy_level)
        if privacy is PrivacyLevel.L3:
            raise EgressBlocked("l3_egress_blocked")
        selected_route = LLMRoute.PRIVATE if privacy is PrivacyLevel.L2 else LLMRoute(request.route)
        routed_request = CompletionRequest.model_validate(
            {**request.model_dump(mode="python"), "route": selected_route}
        )
        policy = self._routes[selected_route]
        rejected_for_privacy = 0
        failures = 0
        for endpoint_name in [policy.primary, *policy.fallbacks]:
            endpoint = self._endpoints[endpoint_name]
            try:
                self._egress.authorize(
                    privacy,
                    EgressDestination(
                        name=endpoint_name,
                        runs_local=endpoint.runs_local,
                        max_privacy_level=PrivacyLevel(endpoint.max_privacy_level),
                    ),
                )
            except EgressBlocked:
                rejected_for_privacy += 1
                continue
            provider = self._providers[endpoint_name]
            for attempt in range(1, endpoint.max_retries + 2):
                timeout_ms = policy.timeout_ms or endpoint.timeout_ms
                try:
                    async with self._span(
                        routed_request,
                        endpoint_name,
                        attempt,
                    ):
                        return await asyncio.wait_for(
                            provider.complete(routed_request),
                            timeout=timeout_ms / 1_000,
                        )
                except TimeoutError:
                    failures += 1
                except Exception:
                    failures += 1
        if rejected_for_privacy and not failures:
            raise LLMRouteExhausted("no_privacy_compatible_model")
        raise LLMRouteExhausted("all_model_routes_failed")

    def _validate_configuration(self) -> None:
        missing_routes = set(LLMRoute) - self._routes.keys()
        if missing_routes:
            raise ValueError("dialogue, utility and private routes are required")
        for route, policy in self._routes.items():
            for endpoint_name in [policy.primary, *policy.fallbacks]:
                if endpoint_name not in self._endpoints or endpoint_name not in self._providers:
                    raise ValueError(f"route references unknown endpoint: {endpoint_name}")
                endpoint = self._endpoints[endpoint_name]
                if route == LLMRoute.PRIVATE and not endpoint.runs_local:
                    raise ValueError("private route cannot reference a cloud endpoint")

    @asynccontextmanager
    async def _span(
        self,
        request: CompletionRequest,
        endpoint_name: str,
        attempt: int,
    ) -> AsyncIterator[None]:
        if self._traces is None:
            yield
            return
        async with self._traces.span(
            request.trace_id,
            "llm.completion",
            {
                "endpoint": endpoint_name,
                "route": request.route,
                "privacy_level": request.privacy_level,
                "attempt": attempt,
            },
        ):
            yield
