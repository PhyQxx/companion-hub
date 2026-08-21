from __future__ import annotations

import json
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
    LLMRoute,
    LLMRouter,
    ModelEndpoint,
    RoutePolicy,
    ToolDefinition,
)


def _endpoint(*, supports_tools: bool) -> ModelEndpoint:
    return ModelEndpoint(
        provider="openai_compatible",
        model="test-model",
        base_url="https://models.example/v1",
        secret_ref="env:TEST_MODEL_KEY",
        runs_local=False,
        max_privacy_level="L1",
        supports_tool_calling=supports_tools,
    )


def _tool_request() -> CompletionRequest:
    return CompletionRequest(
        trace_id=uuid7(),
        messages=[{"role": "user", "content": "济南天气"}],
        privacy_level="L1",
        route="dialogue",
        tools=[
            ToolDefinition(
                name="get_weather",
                description="查询天气",
                parameters={"type": "object", "properties": {}},
            )
        ],
    )


async def test_litellm_complete_maps_tool_schema_and_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_completion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(
            id="request-id",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="call-1",
                                function=SimpleNamespace(
                                    name="get_weather",
                                    arguments='{"location":"济南市"}',
                                ),
                            )
                        ],
                    ),
                    finish_reason="tool_calls",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3),
        )

    monkeypatch.setattr("app.llm.provider.litellm.acompletion", fake_completion)
    provider = LiteLLMProvider(
        "cloud",
        _endpoint(supports_tools=True),
        EnvSecretProvider({"TEST_MODEL_KEY": "test-key"}),
    )

    result = await provider.complete(_tool_request())

    assert captured["tool_choice"] == "auto"
    assert captured["tools"][0]["function"]["name"] == "get_weather"
    assert result.text == ""
    assert result.tool_calls[0].function.arguments == {"location": "济南市"}


async def test_litellm_serializes_followup_tool_messages_as_protocol_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_completion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(
            id="followup-id",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="济南多云 32 度", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=8, completion_tokens=6),
        )

    monkeypatch.setattr("app.llm.provider.litellm.acompletion", fake_completion)
    provider = LiteLLMProvider(
        "cloud",
        _endpoint(supports_tools=True),
        EnvSecretProvider({"TEST_MODEL_KEY": "test-key"}),
    )
    request = CompletionRequest(
        trace_id=uuid7(),
        messages=[
            {"role": "user", "content": "济南天气"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "get_weather",
                            "arguments": {"location": "济南市"},
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "content": '{"weather": "多云"}',
                "tool_call_id": "call-1",
                "name": "get_weather",
            },
        ],
        privacy_level="L1",
        route="dialogue",
    )

    await provider.complete(request)

    assistant_message = captured["messages"][1]
    tool_message = captured["messages"][2]
    assert assistant_message["role"] == "assistant"
    raw_arguments = assistant_message["tool_calls"][0]["function"]["arguments"]
    assert isinstance(raw_arguments, str)
    assert json.loads(raw_arguments) == {"location": "济南市"}
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call-1"
    assert tool_message["name"] == "get_weather"


async def test_tool_capable_provider_probe_requires_a_real_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_completion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(
            id="probe-id",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="probe-call",
                                function=SimpleNamespace(
                                    name="report_probe", arguments="{}"
                                ),
                            )
                        ],
                    ),
                    finish_reason="tool_calls",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
        )

    monkeypatch.setattr("app.llm.provider.litellm.acompletion", fake_completion)
    provider = LiteLLMProvider(
        "cloud",
        _endpoint(supports_tools=True),
        EnvSecretProvider({"TEST_MODEL_KEY": "test-key"}),
    )

    await provider.probe()

    assert captured["tools"][0]["function"]["name"] == "report_probe"


async def test_litellm_stream_aggregates_fragmented_tool_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def chunks() -> Any:
        yield SimpleNamespace(
            id="stream-id",
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call-1",
                                function=SimpleNamespace(
                                    name="get_weather", arguments='{"location":'
                                ),
                            )
                        ],
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
                    delta=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id=None,
                                function=SimpleNamespace(name=None, arguments='"济南市"}'),
                            )
                        ],
                    ),
                    finish_reason="tool_calls",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3),
        )

    async def fake_completion(**kwargs: Any) -> Any:
        del kwargs
        return chunks()

    monkeypatch.setattr("app.llm.provider.litellm.acompletion", fake_completion)
    provider = LiteLLMProvider(
        "cloud",
        _endpoint(supports_tools=True),
        EnvSecretProvider({"TEST_MODEL_KEY": "test-key"}),
    )
    visible: list[str] = []

    async def on_delta(delta: str) -> None:
        visible.append(delta)

    result = await provider.stream(_tool_request(), on_delta)

    assert visible == ["", ""]
    assert result.tool_calls[0].function.arguments == {"location": "济南市"}


class _FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        return CompletionResult(
            text="ok",
            provider="openai_compatible",
            model="test-model",
            endpoint=self.name,
            route=request.route,
            latency_ms=1,
        )

    async def stream(
        self,
        request: CompletionRequest,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> CompletionResult:
        result = await self.complete(request)
        await on_delta(result.text)
        return result

    async def probe(self) -> None:
        return None


async def test_router_skips_endpoints_without_tool_capability() -> None:
    unsupported = _FakeProvider("unsupported")
    capable = _FakeProvider("capable")
    private = _FakeProvider("private")
    private_endpoint = _endpoint(supports_tools=False).model_copy(
        update={
            "base_url": "http://127.0.0.1:11434/v1",
            "secret_ref": None,
            "runs_local": True,
            "max_privacy_level": "L2",
        }
    )
    router = LLMRouter(
        endpoints={
            "unsupported": _endpoint(supports_tools=False),
            "capable": _endpoint(supports_tools=True),
            "private": private_endpoint,
        },
        routes={
            LLMRoute.DIALOGUE: RoutePolicy(
                primary="unsupported", fallbacks=["capable"]
            ),
            LLMRoute.UTILITY: RoutePolicy(primary="capable"),
            LLMRoute.PRIVATE: RoutePolicy(primary="private"),
        },
        providers={
            "unsupported": unsupported,
            "capable": capable,
            "private": private,
        },
    )

    result = await router.complete(_tool_request())

    assert result.endpoint == "capable"
    assert unsupported.requests == []
    assert len(capable.requests) == 1
