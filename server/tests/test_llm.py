from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

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

    assert captured["model"] == "openai/test-model"
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


def _append_to(target: list[str]) -> Callable[[str], Awaitable[None]]:
    async def append(value: str) -> None:
        target.append(value)

    return append
