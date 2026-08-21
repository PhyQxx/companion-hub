from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
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
_DEFAULT_STREAM_IDLE_TIMEOUT_MS = 10_000


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
        rejected_for_tools = 0
        failures = 0
        failure_details: list[LLMEndpointFailure] = []
        for endpoint_name in [policy.primary, *policy.fallbacks]:
            endpoint = self._endpoints[endpoint_name]
            if routed_request.tools and not endpoint.supports_tool_calling:
                rejected_for_tools += 1
                continue
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
            endpoint_request = self._apply_endpoint_max_tokens(
                routed_request, endpoint
            )
            for attempt in range(1, endpoint.max_retries + 2):
                timeout_ms = policy.timeout_ms or endpoint.timeout_ms
                try:
                    async with self._span(
                        endpoint_request,
                        endpoint_name,
                        attempt,
                    ):
                        return await asyncio.wait_for(
                            provider.complete(endpoint_request),
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
                    will_retry = _should_retry_endpoint(
                        error,
                        attempt=attempt,
                        max_attempts=endpoint.max_retries + 1,
                    )
                    self._log_endpoint_failure(
                        request=routed_request,
                        endpoint_name=endpoint_name,
                        endpoint=endpoint,
                        attempt=attempt,
                        max_attempts=endpoint.max_retries + 1,
                        timeout_ms=timeout_ms,
                        error=error,
                        will_retry=will_retry,
                    )
                    if not will_retry:
                        break
                except Exception as error:
                    failures += 1
                    failure_details.append(
                        LLMEndpointFailure(
                            endpoint=endpoint_name,
                            attempt=attempt,
                            error_type=type(error).__name__,
                        )
                    )
                    will_retry = _should_retry_endpoint(
                        error,
                        attempt=attempt,
                        max_attempts=endpoint.max_retries + 1,
                    )
                    self._log_endpoint_failure(
                        request=routed_request,
                        endpoint_name=endpoint_name,
                        endpoint=endpoint,
                        attempt=attempt,
                        max_attempts=endpoint.max_retries + 1,
                        timeout_ms=timeout_ms,
                        error=error,
                        will_retry=will_retry,
                    )
                    if not will_retry:
                        break
        if rejected_for_tools and not failures:
            raise LLMRouteExhausted("tool_model_unavailable")
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
        rejected_for_tools = 0
        failures = 0
        failure_details: list[LLMEndpointFailure] = []
        for endpoint_name in [policy.primary, *policy.fallbacks]:
            endpoint = self._endpoints[endpoint_name]
            if routed_request.tools and not endpoint.supports_tool_calling:
                rejected_for_tools += 1
                continue
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
            endpoint_request = self._apply_endpoint_max_tokens(
                routed_request, endpoint
            )
            for attempt in range(1, endpoint.max_retries + 2):
                emitted = False

                async def guarded_delta(delta: str) -> None:
                    nonlocal emitted
                    if not delta:
                        return
                    emitted = True
                    await on_delta(delta)

                timeout_ms = policy.timeout_ms or endpoint.timeout_ms
                first_chunk_ms = policy.stream_first_chunk_timeout_ms or timeout_ms
                idle_ms = policy.stream_idle_timeout_ms or _DEFAULT_STREAM_IDLE_TIMEOUT_MS
                watchdog = asyncio.timeout(first_chunk_ms / 1_000)
                try:
                    async with self._span(endpoint_request, endpoint_name, attempt), watchdog:
                        return await provider.stream(
                            endpoint_request,
                            _watched_delta(guarded_delta, watchdog, idle_ms / 1_000),
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
                    will_retry = _should_retry_endpoint(
                        error,
                        attempt=attempt,
                        max_attempts=endpoint.max_retries + 1,
                    )
                    timeout_detail = None
                    if isinstance(error, TimeoutError):
                        timeout_detail = (
                            f"stream idle over {idle_ms}ms after first chunk"
                            if emitted
                            else f"no first chunk within {first_chunk_ms}ms"
                        )
                    self._log_endpoint_failure(
                        request=routed_request,
                        endpoint_name=endpoint_name,
                        endpoint=endpoint,
                        attempt=attempt,
                        max_attempts=endpoint.max_retries + 1,
                        timeout_ms=timeout_ms,
                        error=error,
                        will_retry=will_retry,
                        detail_override=timeout_detail,
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
                    if not will_retry:
                        break
        if rejected_for_tools and not failures:
            raise LLMRouteExhausted("tool_model_unavailable")
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
        will_retry: bool,
        detail_override: str | None = None,
    ) -> None:
        status_code = getattr(error, "status_code", None)
        detail = detail_override or _safe_error_detail(error)
        if isinstance(error, TimeoutError) and not detail:
            detail = f"request timed out after {timeout_ms}ms"
        if isinstance(error, TimeoutError):
            cn_note = "【请求超时, 降级下一端点】"
        elif status_code == 429 or type(error).__name__ == "RateLimitError":
            cn_note = "【触发速率限制, 降级下一端点】"
        else:
            cn_note = "【请求失败, 降级下一端点】"
        logger.warning(
            "%s llm endpoint failed trace_id=%s route=%s privacy=%s endpoint=%s "
            "provider=%s model=%s attempt=%s/%s error_type=%s status_code=%s "
            "next_action=%s detail=%s",
            cn_note,
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
            "retry_same_endpoint" if will_retry else "fallback_next_endpoint",
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
            "【该路由所有端点均失败, 请检查模型配置或网络状况】"
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

    @staticmethod
    def _apply_endpoint_max_tokens(
        request: CompletionRequest,
        endpoint: ModelEndpoint,
    ) -> CompletionRequest:
        if endpoint.max_tokens is None:
            return request
        return CompletionRequest.model_validate(
            {**request.model_dump(mode="python"), "max_tokens": endpoint.max_tokens}
        )

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


def _watched_delta(
    on_delta: Callable[[str], Awaitable[None]],
    watchdog: asyncio.Timeout,
    idle_seconds: float,
) -> Callable[[str], Awaitable[None]]:
    """Wrap on_delta so every chunk postpones the stream watchdog deadline.

    asyncio.timeout caps total duration and cannot be extended, but its Timeout
    object exposes reschedule(): each arriving delta moves the deadline to
    "now + idle_seconds", turning the cap into an idle-gap watchdog. The stream
    is only cancelled when output truly stops (late first chunk or a stalled
    generation), never merely because a long reply is still producing tokens.
    """

    async def watched(delta: str) -> None:
        with suppress(RuntimeError):
            # 看门狗此刻已到期且任务取消已在路上, 挂起的取消会照常触发超时
            watchdog.reschedule(asyncio.get_running_loop().time() + idle_seconds)
        await on_delta(delta)

    return watched


def _safe_error_detail(error: BaseException) -> str:
    """Return a bounded diagnostic message while stripping common secret shapes."""
    detail = " ".join(str(error).split())
    for pattern in _SECRET_PATTERNS:
        detail = pattern.sub(r"\1<redacted>" if pattern.groups else "<redacted>", detail)
    if len(detail) > _MAX_ERROR_DETAIL_CHARS:
        detail = detail[:_MAX_ERROR_DETAIL_CHARS] + "…"
    return detail


def _should_retry_endpoint(
    error: BaseException,
    *,
    attempt: int,
    max_attempts: int,
) -> bool:
    """Retry only failures where another attempt at the same endpoint can help.

    Rate limits and route-level timeouts should fail over immediately. Retrying a
    known 429 only burns more quota, while repeating a full timeout makes voice
    interactions stall for tens of seconds before fallback is even attempted.
    Other provider failures retain the configured bounded retry behavior.
    """
    if attempt >= max_attempts:
        return False
    if isinstance(error, TimeoutError):
        return False
    status_code = getattr(error, "status_code", None)
    return status_code != 429 and type(error).__name__ != "RateLimitError"
