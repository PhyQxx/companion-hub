from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass

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

logger = logging.getLogger(__name__)

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|token|secret)\s*[:=]\s*)"
        r"['\"]?[^'\"\s,;}]+"
    ),
    re.compile(r"(?i)([?&](?:api_key|key|token|access_token)=)[^&\s]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)
_MAX_ERROR_DETAIL_CHARS = 1_200


@dataclass(frozen=True, slots=True)
class LLMEndpointFailure:
    endpoint: str
    attempt: int
    error_type: str


class LLMRouteExhausted(RuntimeError):
    def __init__(
        self,
        reason_code: str,
        *,
        failures: tuple[LLMEndpointFailure, ...] = (),
    ) -> None:
        self.reason_code = reason_code
        self.failures = failures
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
        failure_details: list[LLMEndpointFailure] = []
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
                except TimeoutError as error:
                    failures += 1
                    failure_details.append(
                        LLMEndpointFailure(
                            endpoint=endpoint_name,
                            attempt=attempt,
                            error_type="TimeoutError",
                        )
                    )
                    self._log_endpoint_failure(
                        request=routed_request,
                        endpoint_name=endpoint_name,
                        endpoint=endpoint,
                        attempt=attempt,
                        max_attempts=endpoint.max_retries + 1,
                        timeout_ms=timeout_ms,
                        error=error,
                    )
                except Exception as error:
                    failures += 1
                    failure_details.append(
                        LLMEndpointFailure(
                            endpoint=endpoint_name,
                            attempt=attempt,
                            error_type=type(error).__name__,
                        )
                    )
                    self._log_endpoint_failure(
                        request=routed_request,
                        endpoint_name=endpoint_name,
                        endpoint=endpoint,
                        attempt=attempt,
                        max_attempts=endpoint.max_retries + 1,
                        timeout_ms=timeout_ms,
                        error=error,
                    )
        if rejected_for_privacy and not failures:
            logger.error(
                "llm route rejected by privacy guard trace_id=%s route=%s privacy=%s",
                routed_request.trace_id,
                selected_route.value,
                privacy.value,
            )
            raise LLMRouteExhausted("no_privacy_compatible_model")
        self._log_route_exhausted(
            request=routed_request,
            reason_code="all_model_routes_failed",
            failures=failure_details,
        )
        raise LLMRouteExhausted(
            "all_model_routes_failed",
            failures=tuple(failure_details),
        )

    async def stream(
        self,
        request: CompletionRequest,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> CompletionResult:
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
        failure_details: list[LLMEndpointFailure] = []
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
                emitted = False

                async def guarded_delta(delta: str) -> None:
                    nonlocal emitted
                    emitted = True
                    await on_delta(delta)

                timeout_ms = policy.timeout_ms or endpoint.timeout_ms
                try:
                    async with self._span(routed_request, endpoint_name, attempt):
                        async with asyncio.timeout(timeout_ms / 1_000):
                            return await provider.stream(routed_request, guarded_delta)
                except Exception as error:
                    failures += 1
                    failure_details.append(
                        LLMEndpointFailure(
                            endpoint=endpoint_name,
                            attempt=attempt,
                            error_type=type(error).__name__,
                        )
                    )
                    self._log_endpoint_failure(
                        request=routed_request,
                        endpoint_name=endpoint_name,
                        endpoint=endpoint,
                        attempt=attempt,
                        max_attempts=endpoint.max_retries + 1,
                        timeout_ms=timeout_ms,
                        error=error,
                    )
                    if emitted:
                        self._log_route_exhausted(
                            request=routed_request,
                            reason_code="stream_interrupted",
                            failures=failure_details,
                        )
                        raise LLMRouteExhausted(
                            "stream_interrupted",
                            failures=tuple(failure_details),
                        ) from None
        if rejected_for_privacy and not failures:
            logger.error(
                "llm route rejected by privacy guard trace_id=%s route=%s privacy=%s",
                routed_request.trace_id,
                selected_route.value,
                privacy.value,
            )
            raise LLMRouteExhausted("no_privacy_compatible_model")
        self._log_route_exhausted(
            request=routed_request,
            reason_code="all_model_routes_failed",
            failures=failure_details,
        )
        raise LLMRouteExhausted(
            "all_model_routes_failed",
            failures=tuple(failure_details),
        )

    def _log_endpoint_failure(
        self,
        *,
        request: CompletionRequest,
        endpoint_name: str,
        endpoint: ModelEndpoint,
        attempt: int,
        max_attempts: int,
        timeout_ms: int,
        error: BaseException,
    ) -> None:
        status_code = getattr(error, "status_code", None)
        detail = _safe_error_detail(error)
        if isinstance(error, TimeoutError) and not detail:
            detail = f"request timed out after {timeout_ms}ms"
        logger.warning(
            "llm endpoint failed trace_id=%s route=%s privacy=%s endpoint=%s "
            "provider=%s model=%s attempt=%s/%s error_type=%s status_code=%s detail=%s",
            request.trace_id,
            request.route,
            request.privacy_level,
            endpoint_name,
            endpoint.provider,
            endpoint.model,
            attempt,
            max_attempts,
            type(error).__name__,
            status_code if status_code is not None else "-",
            detail or "-",
        )

    @staticmethod
    def _log_route_exhausted(
        *,
        request: CompletionRequest,
        reason_code: str,
        failures: list[LLMEndpointFailure],
    ) -> None:
        summary = ", ".join(
            f"{item.endpoint}#{item.attempt}:{item.error_type}" for item in failures
        )
        logger.error(
            "llm route exhausted trace_id=%s route=%s privacy=%s reason=%s failures=[%s]",
            request.trace_id,
            request.route,
            request.privacy_level,
            reason_code,
            summary,
        )

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


def _safe_error_detail(error: BaseException) -> str:
    """Return a bounded diagnostic message while stripping common secret shapes."""
    detail = " ".join(str(error).split())
    for pattern in _SECRET_PATTERNS:
        detail = pattern.sub(r"\1<redacted>" if pattern.groups else "<redacted>", detail)
    if len(detail) > _MAX_ERROR_DETAIL_CHARS:
        detail = detail[:_MAX_ERROR_DETAIL_CHARS] + "…"
    return detail
