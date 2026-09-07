from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.ids import uuid7
from app.llm import (
    CompletionRequest,
    CompletionResult,
    EnvSecretProvider,
    LiteLLMProvider,
    LLMEndpointFailure,
    LLMRoute,
    LLMRouteExhausted,
    LLMRouter,
    ModelEndpoint,
    RoutePolicy,
    SecretNotFound,
)
from app.observability import InMemorySpanSink, TraceRecorder
from app.privacy import EgressBlocked


def endpoint(*, local: bool, max_privacy: str = "L1", retries: int = 0) -> ModelEndpoint:
    return ModelEndpoint(
        provider="openai_compatible",
        model="test-model",
        base_url="http://127.0.0.1:11434/v1" if local else "https://models.example/v1",
        secret_ref=None if local else "env:TEST_MODEL_KEY",
        runs_local=local,
        max_privacy_level=max_privacy,
        max_retries=retries,
        max_context_tokens=32_768,
        input_cost_per_million=0,
        output_cost_per_million=0,
    )


class FakeProvider:
    def __init__(
        self, name: str, *, fail: bool = False, fail_after_delta: bool = False
    ) -> None:
        self.name = name
        self.fail = fail
        self.fail_after_delta = fail_after_delta
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("provider_failed_with_private_payload")
        return CompletionResult(
            text="ok",
            provider="openai_compatible",
            model="test-model",
            endpoint=self.name,
            route=request.route,
            latency_ms=1,
        )

    async def probe(self) -> None:
        return None

    async def stream(
        self,
        request: CompletionRequest,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> CompletionResult:
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("provider_failed_before_delta")
        await on_delta(f"{self.name}-delta")
        if self.fail_after_delta:
            raise RuntimeError("provider_failed_after_delta")
        return CompletionResult(
            text=f"{self.name}-delta",
            provider="openai_compatible",
            model="test-model",
            endpoint=self.name,
            route=request.route,
            latency_ms=1,
        )


def request(level: str, *, route: str = "dialogue") -> CompletionRequest:
    return CompletionRequest(
        trace_id=uuid7(),
        messages=[{"role": "user", "content": "private test phrase"}],
        privacy_level=level,
        route=route,
    )


def router(
    *,
    cloud: FakeProvider | None = None,
    private: FakeProvider | None = None,
    traces: TraceRecorder | None = None,
) -> LLMRouter:
    cloud = cloud or FakeProvider("cloud")
    private = private or FakeProvider("private")
    return LLMRouter(
        endpoints={
            "cloud": endpoint(local=False),
            "private": endpoint(local=True, max_privacy="L2"),
        },
        routes={
            LLMRoute.DIALOGUE: RoutePolicy(primary="cloud"),
            LLMRoute.UTILITY: RoutePolicy(primary="cloud"),
            LLMRoute.PRIVATE: RoutePolicy(primary="private"),
        },
        providers={"cloud": cloud, "private": private},
        traces=traces,
    )


async def test_l2_is_forced_to_private_route_without_cloud_egress() -> None:
    cloud = FakeProvider("cloud")
    private = FakeProvider("private")
    result = await router(cloud=cloud, private=private).complete(request("L2"))

    assert result.endpoint == "private"
    assert result.route == "private"
    assert cloud.requests == []
    assert private.requests[0].route == "private"


async def test_voice_route_falls_back_to_dialogue_when_not_configured() -> None:
    cloud = FakeProvider("cloud")

    result = await router(cloud=cloud).complete(request("L1", route="voice"))

    assert result.endpoint == "cloud"
    assert result.route == "dialogue"
    assert cloud.requests[0].route == "dialogue"


async def test_voice_route_uses_dedicated_policy_when_configured() -> None:
    dialogue = FakeProvider("dialogue")
    voice = FakeProvider("voice-fast")
    private = FakeProvider("private")
    instance = LLMRouter(
        endpoints={
            "dialogue": endpoint(local=False),
            "voice-fast": endpoint(local=False),
            "private": endpoint(local=True, max_privacy="L2"),
        },
        routes={
            LLMRoute.DIALOGUE: RoutePolicy(primary="dialogue"),
            LLMRoute.VOICE: RoutePolicy(primary="voice-fast"),
            LLMRoute.UTILITY: RoutePolicy(primary="dialogue"),
            LLMRoute.PRIVATE: RoutePolicy(primary="private"),
        },
        providers={"dialogue": dialogue, "voice-fast": voice, "private": private},
    )

    result = await instance.complete(request("L1", route="voice"))

    assert result.endpoint == "voice-fast"
    assert result.route == "voice"
    assert dialogue.requests == []
    assert voice.requests[0].route == "voice"


async def test_l3_is_blocked_before_any_provider_call() -> None:
    cloud = FakeProvider("cloud")
    private = FakeProvider("private")

    with pytest.raises(EgressBlocked, match="l3_egress_blocked"):
        await router(cloud=cloud, private=private).complete(request("L3"))

    assert cloud.requests == []
    assert private.requests == []


async def test_fallback_and_retry_are_bounded() -> None:
    failed = FakeProvider("failed", fail=True)
    fallback = FakeProvider("fallback")
    instance = LLMRouter(
        endpoints={
            "failed": endpoint(local=False, retries=1),
            "fallback": endpoint(local=False),
            "private": endpoint(local=True, max_privacy="L2"),
        },
        routes={
            LLMRoute.DIALOGUE: RoutePolicy(primary="failed", fallbacks=["fallback"]),
            LLMRoute.UTILITY: RoutePolicy(primary="fallback"),
            LLMRoute.PRIVATE: RoutePolicy(primary="private"),
        },
        providers={
            "failed": failed,
            "fallback": fallback,
            "private": FakeProvider("private"),
        },
    )

    result = await instance.complete(request("L1"))
    assert result.endpoint == "fallback"
    assert len(failed.requests) == 2
    assert len(fallback.requests) == 1


async def test_rate_limit_skips_same_endpoint_retry_and_falls_back_immediately() -> None:
    class RateLimitedProvider(FakeProvider):
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            self.requests.append(request)
            error = RuntimeError("rate limited")
            error.status_code = 429  # type: ignore[attr-defined]
            raise error

    limited = RateLimitedProvider("limited")
    fallback = FakeProvider("fallback")
    instance = LLMRouter(
        endpoints={
            "limited": endpoint(local=False, retries=2),
            "fallback": endpoint(local=False),
            "private": endpoint(local=True, max_privacy="L2"),
        },
        routes={
            LLMRoute.DIALOGUE: RoutePolicy(primary="limited", fallbacks=["fallback"]),
            LLMRoute.UTILITY: RoutePolicy(primary="fallback"),
            LLMRoute.PRIVATE: RoutePolicy(primary="private"),
        },
        providers={
            "limited": limited,
            "fallback": fallback,
            "private": FakeProvider("private"),
        },
    )

    result = await instance.complete(request("L1"))

    assert result.endpoint == "fallback"
    assert len(limited.requests) == 1
    assert len(fallback.requests) == 1


async def test_timeout_skips_same_endpoint_retry_and_falls_back_immediately() -> None:
    class TimeoutProvider(FakeProvider):
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            self.requests.append(request)
            raise TimeoutError("synthetic timeout")

    timed_out = TimeoutProvider("timed-out")
    fallback = FakeProvider("fallback")
    instance = LLMRouter(
        endpoints={
            "timed-out": endpoint(local=False, retries=2),
            "fallback": endpoint(local=False),
            "private": endpoint(local=True, max_privacy="L2"),
        },
        routes={
            LLMRoute.DIALOGUE: RoutePolicy(primary="timed-out", fallbacks=["fallback"]),
            LLMRoute.UTILITY: RoutePolicy(primary="fallback"),
            LLMRoute.PRIVATE: RoutePolicy(primary="private"),
        },
        providers={
            "timed-out": timed_out,
            "fallback": fallback,
            "private": FakeProvider("private"),
        },
    )

    result = await instance.complete(request("L1"))

    assert result.endpoint == "fallback"
    assert len(timed_out.requests) == 1
    assert len(fallback.requests) == 1


async def test_exhausted_route_keeps_safe_failure_diagnostics_only() -> None:
    first = FakeProvider("first", fail=True)
    fallback = FakeProvider("fallback", fail=True)
    instance = LLMRouter(
        endpoints={
            "first": endpoint(local=False, retries=1),
            "fallback": endpoint(local=False),
            "private": endpoint(local=True, max_privacy="L2"),
        },
        routes={
            LLMRoute.DIALOGUE: RoutePolicy(primary="first", fallbacks=["fallback"]),
            LLMRoute.UTILITY: RoutePolicy(primary="fallback"),
            LLMRoute.PRIVATE: RoutePolicy(primary="private"),
        },
        providers={
            "first": first,
            "fallback": fallback,
            "private": FakeProvider("private"),
        },
    )

    with pytest.raises(LLMRouteExhausted) as captured:
        await instance.complete(request("L1"))

    assert captured.value.reason_code == "all_model_routes_failed"
    assert captured.value.failures == (
        LLMEndpointFailure("first", 1, "RuntimeError"),
        LLMEndpointFailure("first", 2, "RuntimeError"),
        LLMEndpointFailure("fallback", 1, "RuntimeError"),
    )
    serialized = repr(captured.value.failures)
    assert "provider_failed_with_private_payload" not in serialized
    assert "private test phrase" not in serialized


async def test_route_failure_logs_actionable_reason_without_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class DiagnosticProvider(FakeProvider):
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            self.requests.append(request)
            raise RuntimeError(
                "status=429 too many requests api_key=super-secret-key "
                "Authorization: Bearer bearer-secret"
            )

    failing = DiagnosticProvider("cloud")
    instance = router(cloud=failing)
    caplog.set_level(logging.WARNING, logger="app.llm.router")

    with pytest.raises(LLMRouteExhausted):
        await instance.complete(request("L1"))

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "llm endpoint failed" in logs
    assert "all_model_routes_failed" in logs
    assert "status=429 too many requests" in logs
    assert "RuntimeError" in logs
    assert "super-secret-key" not in logs
    assert "bearer-secret" not in logs
    assert "<redacted>" in logs
    assert "private test phrase" not in logs


async def test_stream_falls_back_only_before_first_visible_delta() -> None:
    failed = FakeProvider("failed", fail=True)
    fallback = FakeProvider("fallback")
    instance = LLMRouter(
        endpoints={
            "failed": endpoint(local=False),
            "fallback": endpoint(local=False),
            "private": endpoint(local=True, max_privacy="L2"),
        },
        routes={
            LLMRoute.DIALOGUE: RoutePolicy(primary="failed", fallbacks=["fallback"]),
            LLMRoute.UTILITY: RoutePolicy(primary="fallback"),
            LLMRoute.PRIVATE: RoutePolicy(primary="private"),
        },
        providers={
            "failed": failed,
            "fallback": fallback,
            "private": FakeProvider("private"),
        },
    )
    deltas: list[str] = []

    result = await instance.stream(request("L1"), _append_to(deltas))

    assert result.endpoint == "fallback"
    assert deltas == ["fallback-delta"]


async def test_stream_rate_limit_skips_same_endpoint_retry_before_first_delta() -> None:
    class RateLimitedStreamProvider(FakeProvider):
        async def stream(
            self,
            request: CompletionRequest,
            on_delta: Callable[[str], Awaitable[None]],
        ) -> CompletionResult:
            del on_delta
            self.requests.append(request)
            error = RuntimeError("rate limited")
            error.status_code = 429  # type: ignore[attr-defined]
            raise error

    limited = RateLimitedStreamProvider("limited")
    fallback = FakeProvider("fallback")
    instance = LLMRouter(
        endpoints={
            "limited": endpoint(local=False, retries=2),
            "fallback": endpoint(local=False),
            "private": endpoint(local=True, max_privacy="L2"),
        },
        routes={
            LLMRoute.DIALOGUE: RoutePolicy(primary="limited", fallbacks=["fallback"]),
            LLMRoute.UTILITY: RoutePolicy(primary="fallback"),
            LLMRoute.PRIVATE: RoutePolicy(primary="private"),
        },
        providers={
            "limited": limited,
            "fallback": fallback,
            "private": FakeProvider("private"),
        },
    )
    deltas: list[str] = []

    result = await instance.stream(request("L1"), _append_to(deltas))

    assert result.endpoint == "fallback"
    assert len(limited.requests) == 1
    assert len(fallback.requests) == 1
    assert deltas == ["fallback-delta"]


async def test_stream_never_splices_fallback_after_visible_delta() -> None:
    partial = FakeProvider("partial", fail_after_delta=True)
    fallback = FakeProvider("fallback")
    instance = LLMRouter(
        endpoints={
            "partial": endpoint(local=False),
            "fallback": endpoint(local=False),
            "private": endpoint(local=True, max_privacy="L2"),
        },
        routes={
            LLMRoute.DIALOGUE: RoutePolicy(primary="partial", fallbacks=["fallback"]),
            LLMRoute.UTILITY: RoutePolicy(primary="fallback"),
            LLMRoute.PRIVATE: RoutePolicy(primary="private"),
        },
        providers={
            "partial": partial,
            "fallback": fallback,
            "private": FakeProvider("private"),
        },
    )
    deltas: list[str] = []

    with pytest.raises(LLMRouteExhausted) as captured:
        await instance.stream(request("L1"), _append_to(deltas))

    assert captured.value.reason_code == "stream_interrupted"
    assert captured.value.failures == (
        LLMEndpointFailure("partial", 1, "RuntimeError"),
    )
    assert deltas == ["partial-delta"]
    assert fallback.requests == []


def _stream_router(
    *,
    primary: FakeProvider,
    fallback: FakeProvider,
    policy: RoutePolicy,
) -> tuple[LLMRouter, FakeProvider, FakeProvider]:
    instance = LLMRouter(
        endpoints={
            "primary": endpoint(local=False),
            "fallback": endpoint(local=False),
            "private": endpoint(local=True, max_privacy="L2"),
        },
        routes={
            LLMRoute.DIALOGUE: policy,
            LLMRoute.UTILITY: RoutePolicy(primary="fallback"),
            LLMRoute.PRIVATE: RoutePolicy(primary="private"),
        },
        providers={"primary": primary, "fallback": fallback, "private": FakeProvider("private")},
    )
    return instance, primary, fallback


async def test_stream_survives_generation_longer_than_first_chunk_deadline() -> None:
    class TrickleStreamProvider(FakeProvider):
        async def stream(
            self,
            request: CompletionRequest,
            on_delta: Callable[[str], Awaitable[None]],
        ) -> CompletionResult:
            self.requests.append(request)
            chunks = [f"chunk{index} " for index in range(10)]
            for chunk in chunks:
                await asyncio.sleep(0.03)
                await on_delta(chunk)
            return CompletionResult(
                text="".join(chunks),
                provider="openai_compatible",
                model="test-model",
                endpoint=self.name,
                route=request.route,
                latency_ms=1,
            )

    trickle = TrickleStreamProvider("primary")
    # 总时长约 300ms，远超 timeout_ms=100；只要 chunk 间隔小于空闲看门狗，
    # 流就必须完整生成完毕，不允许被总时长上限掐断。
    instance, _, _ = _stream_router(
        primary=trickle,
        fallback=FakeProvider("fallback"),
        policy=RoutePolicy(
            primary="primary",
            fallbacks=["fallback"],
            timeout_ms=100,
            stream_idle_timeout_ms=200,
        ),
    )
    deltas: list[str] = []

    result = await instance.stream(request("L1"), _append_to(deltas))

    assert result.endpoint == "primary"
    assert deltas == [f"chunk{index} " for index in range(10)]


async def test_stream_first_chunk_watchdog_falls_over_before_any_output(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class SlowStartStreamProvider(FakeProvider):
        async def stream(
            self,
            request: CompletionRequest,
            on_delta: Callable[[str], Awaitable[None]],
        ) -> CompletionResult:
            self.requests.append(request)
            await asyncio.sleep(0.35)
            await on_delta("late-delta")
            return CompletionResult(
                text="late-delta",
                provider="openai_compatible",
                model="test-model",
                endpoint=self.name,
                route=request.route,
                latency_ms=1,
            )

    slow = SlowStartStreamProvider("primary")
    fallback = FakeProvider("fallback")
    instance, _, fallback = _stream_router(
        primary=slow,
        fallback=fallback,
        policy=RoutePolicy(
            primary="primary",
            fallbacks=["fallback"],
            stream_first_chunk_timeout_ms=100,
        ),
    )
    caplog.set_level(logging.WARNING, logger="app.llm.router")
    deltas: list[str] = []

    result = await instance.stream(request("L1"), _append_to(deltas))

    assert result.endpoint == "fallback"
    assert deltas == ["fallback-delta"]
    assert "no first chunk within 100ms" in caplog.text


async def test_stream_idle_watchdog_interrupts_stalled_stream_without_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class StalledStreamProvider(FakeProvider):
        async def stream(
            self,
            request: CompletionRequest,
            on_delta: Callable[[str], Awaitable[None]],
        ) -> CompletionResult:
            self.requests.append(request)
            await on_delta("first-delta")
            await asyncio.sleep(0.35)
            await on_delta("never-delta")
            return CompletionResult(
                text="first-deltanever-delta",
                provider="openai_compatible",
                model="test-model",
                endpoint=self.name,
                route=request.route,
                latency_ms=1,
            )

    stalled = StalledStreamProvider("primary")
    fallback = FakeProvider("fallback")
    instance, _, fallback = _stream_router(
        primary=stalled,
        fallback=fallback,
        policy=RoutePolicy(
            primary="primary",
            fallbacks=["fallback"],
            stream_idle_timeout_ms=100,
        ),
    )
    caplog.set_level(logging.WARNING, logger="app.llm.router")
    deltas: list[str] = []

    with pytest.raises(LLMRouteExhausted) as captured:
        await instance.stream(request("L1"), _append_to(deltas))

    assert captured.value.reason_code == "stream_interrupted"
    assert captured.value.failures[-1] == LLMEndpointFailure("primary", 1, "TimeoutError")
    assert deltas == ["first-delta"]
    assert fallback.requests == []
    assert "stream idle over 100ms after first chunk" in caplog.text


async def test_trace_records_safe_metadata_only() -> None:
    sink = InMemorySpanSink()
    await router(traces=TraceRecorder(sink)).complete(request("L1"))

    serialized = repr(sink.records)
    assert "private test phrase" not in serialized
    assert sink.records[0].attributes == {
        "endpoint": "cloud",
        "route": "dialogue",
        "privacy_level": "L1",
        "attempt": 1,
    }


def test_private_route_rejects_cloud_endpoint() -> None:
    with pytest.raises(ValueError, match="private route cannot reference"):
        LLMRouter(
            endpoints={"cloud": endpoint(local=False)},
            routes={
                route: RoutePolicy(primary="cloud")
                for route in LLMRoute
            },
            providers={"cloud": FakeProvider("cloud")},
        )


async def test_privacy_incompatible_endpoint_returns_stable_reason() -> None:
    private = FakeProvider("private")
    instance = LLMRouter(
        endpoints={"private": endpoint(local=True, max_privacy="L1")},
        routes={
            route: RoutePolicy(primary="private")
            for route in LLMRoute
        },
        providers={"private": private},
    )
    with pytest.raises(LLMRouteExhausted) as captured:
        await instance.complete(request("L2"))
    assert captured.value.reason_code == "no_privacy_compatible_model"
    assert private.requests == []


def test_secret_provider_never_falls_back_to_inline_values() -> None:
    secrets = EnvSecretProvider({})
    with pytest.raises(SecretNotFound) as captured:
        secrets.resolve("env:MISSING_KEY")
    assert "MISSING_KEY" in str(captured.value)
    with pytest.raises(SecretNotFound, match="invalid_reference"):
        secrets.resolve("inline:forbidden")


def test_endpoint_rejects_inline_secret_fields() -> None:
    raw: dict[str, Any] = endpoint(local=False).model_dump(mode="json")
    raw["api_key"] = "should-never-be-accepted"
    with pytest.raises(ValueError):
        ModelEndpoint.model_validate(raw)


@pytest.mark.parametrize(
    ("supports_json_mode", "expects_response_format"),
    [(False, False), (True, True)],
)
async def test_litellm_adapter_maps_openai_compatible_contract(
    monkeypatch: pytest.MonkeyPatch,
    *,
    supports_json_mode: bool,
    expects_response_format: bool,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_completion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(
            id="provider-request-id",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"ok":true}'),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=4, completion_tokens=3),
        )

    monkeypatch.setattr("app.llm.provider.litellm.acompletion", fake_completion)
    model = endpoint(local=False).model_copy(
        update={
            "supports_json_mode": supports_json_mode,
            "input_cost_per_million": 1.0,
            "output_cost_per_million": 2.0,
        }
    )
    provider = LiteLLMProvider(
        "cloud", model, EnvSecretProvider({"TEST_MODEL_KEY": "test-only-key"})
    )
    completion_request = request("L0", route="utility").model_copy(
        update={"json_mode": True}
    )
    result = await provider.complete(completion_request)

    assert captured["model"] == "custom_openai/test-model"
    assert captured["api_base"] == "https://models.example/v1"
    assert ("response_format" in captured) is expects_response_format
    assert result.request_id == "provider-request-id"
    assert result.usage.total_tokens == 7
    assert result.usage.estimated_cost == pytest.approx(0.00001)


async def test_litellm_adapter_streams_real_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def chunks() -> Any:
        yield SimpleNamespace(
            id="stream-id",
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="hello "), finish_reason=None
                )
            ],
            usage=None,
        )
        yield SimpleNamespace(
            id="stream-id",
            choices=[
                SimpleNamespace(delta=SimpleNamespace(content="world"), finish_reason="stop")
            ],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
        )

    async def fake_completion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return chunks()

    monkeypatch.setattr("app.llm.provider.litellm.acompletion", fake_completion)
    provider = LiteLLMProvider(
        "cloud",
        endpoint(local=False),
        EnvSecretProvider({"TEST_MODEL_KEY": "test-only-key"}),
    )
    deltas: list[str] = []

    result = await provider.stream(request("L0"), _append_to(deltas))

    assert captured["stream"] is True
    assert deltas == ["hello ", "world"]
    assert result.text == "hello world"
    assert result.finish_reason == "stop"
    assert result.usage.total_tokens == 5


async def test_local_openai_compatible_provider_uses_non_secret_placeholder_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_completion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(
            id="local-request-id",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="OK"),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    monkeypatch.setattr("app.llm.provider.litellm.acompletion", fake_completion)
    provider = LiteLLMProvider(
        "private",
        endpoint(local=True, max_privacy="L2"),
        EnvSecretProvider({}),
    )

    await provider.complete(request("L2"))

    assert captured["api_key"] == "local-no-auth"
    assert captured["api_base"] == "http://127.0.0.1:11434/v1"


async def test_litellm_adapter_passes_explicit_thinking_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_completion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(
            id="thinking-request-id",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="OK"),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    monkeypatch.setattr("app.llm.provider.litellm.acompletion", fake_completion)
    model = endpoint(local=False).model_copy(update={"thinking_mode": "disabled"})
    provider = LiteLLMProvider(
        "cloud",
        model,
        EnvSecretProvider({"TEST_MODEL_KEY": "test-only-key"}),
    )

    await provider.complete(request("L1"))

    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}


async def test_reasoning_overhead_expands_wire_token_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_completion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(
            id="reasoning-request-id",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="OK"), finish_reason="stop"
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    monkeypatch.setattr("app.llm.provider.litellm.acompletion", fake_completion)
    provider = LiteLLMProvider(
        "private",
        endpoint(local=True, max_privacy="L2").model_copy(
            update={"reasoning_overhead_tokens": 2_048}
        ),
        EnvSecretProvider({}),
    )

    await provider.complete(request("L2").model_copy(update={"max_tokens": 512}))

    # 线上预算 = 可见正文预算 + 端点思考开销；默认 0 时严格保持请求预算
    assert captured["max_tokens"] == 512 + 2_048


async def test_stream_ignores_reasoning_content_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def chunks() -> Any:
        yield SimpleNamespace(
            id="stream-id",
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None, reasoning_content="先思考一段推理"
                    ),
                    finish_reason=None,
                )
            ],
            usage=None,
        )
        yield SimpleNamespace(
            id="stream-id",
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="可见正文", reasoning_content=None),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
        )

    async def fake_completion(**kwargs: Any) -> Any:
        return chunks()

    monkeypatch.setattr("app.llm.provider.litellm.acompletion", fake_completion)
    provider = LiteLLMProvider(
        "private", endpoint(local=True, max_privacy="L2"), EnvSecretProvider({})
    )
    deltas: list[str] = []

    result = await provider.stream(request("L2"), _append_to(deltas))

    # LM Studio 将 reasoning_content 与 content 分离；思考增量绝不能进入可见流
    assert deltas == ["可见正文"]
    assert result.text == "可见正文"


def _append_to(target: list[str]) -> Callable[[str], Awaitable[None]]:
    async def append(value: str) -> None:
        target.append(value)

    return append


def test_completion_request_accepts_full_production_tool_catalog() -> None:
    """回归（2026-09-07 生产事故）：J4~J6 上线后挂载工具达 18 个，超过旧的
    max_length=16 导致所有回合在 start_turn 构造 CompletionRequest 时抛
    ValidationError——聊天/语音发消息全部无响应。上限放宽到 32 后，用
    真实生产工具目录全量构造必须通过；33 个仍拒绝（保留卫生上限）。
    """
    from app.cognition.action_registry import build_builtin_action_registry

    # 聊天挂载层的工具集 = 18 个（设备/助手/情境）；这里以注册目录构造即可
    # 覆盖"目录增长不得越过 schema 上限"的约束。
    registry_tools = [
        {"name": item.action_id, "parameters": {}}
        for item in build_builtin_action_registry().definitions()
    ]
    # 18 个是当前真实规模
    assert len(registry_tools) == 18
    tools = [
        {"name": f"tool_{index}", "description": "测试工具", "parameters": {}}
        for index in range(32)
    ]
    request = CompletionRequest(
        trace_id=uuid7(),
        messages=[{"role": "user", "content": "hi"}],
        privacy_level="L1",
        route=LLMRoute.DIALOGUE,
        tools=tools,
    )
    assert len(request.tools) == 32
    with pytest.raises(ValidationError):
        CompletionRequest(
            trace_id=uuid7(),
            messages=[{"role": "user", "content": "hi"}],
            privacy_level="L1",
            route=LLMRoute.DIALOGUE,
            tools=[*tools, {"name": "tool_32", "description": "测试工具", "parameters": {}}],
        )
