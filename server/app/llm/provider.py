from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from time import perf_counter
from typing import Any, Protocol

import litellm

from app.ids import uuid7
from app.observability import redact_fields

from .contracts import (
    CompletionRequest,
    CompletionResult,
    ModelEndpoint,
    ModelUsage,
    ToolCall,
    ToolDefinition,
)

logger = logging.getLogger(__name__)


class SecretNotFound(LookupError):
    def __init__(self, reference: str) -> None:
        super().__init__(f"secret reference is unavailable: {reference}")


class EnvSecretProvider:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else os.environ

    def resolve(self, reference: str) -> str:
        prefix, _, name = reference.partition(":")
        if prefix != "env" or not name:
            raise SecretNotFound("invalid_reference")
        value = self._environ.get(name)
        if not value:
            raise SecretNotFound(reference)
        return value


class LLMProvider(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResult: ...

    async def stream(
        self, request: CompletionRequest, on_delta: Callable[[str], Awaitable[None]]
    ) -> CompletionResult: ...

    async def probe(self) -> None: ...


class LiteLLMProvider:
    """OpenAI-compatible provider behind a provider-neutral contract."""

    def __init__(
        self,
        endpoint_name: str,
        endpoint: ModelEndpoint,
        secrets: EnvSecretProvider,
    ) -> None:
        self.endpoint_name = endpoint_name
        self.endpoint = endpoint
        self._secrets = secrets
        litellm.suppress_debug_info = True

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        started = perf_counter()
        arguments = self._arguments(request)
        self._log_arguments(arguments)
        response = await litellm.acompletion(**arguments)
        choice = response.choices[0]
        content = choice.message.content or ""
        tool_calls = _parse_tool_calls(getattr(choice.message, "tool_calls", None))
        result = self._result(
            request=request,
            text=content,
            request_id=response.id,
            finish_reason=choice.finish_reason,
            usage=response.usage,
            started=started,
            tool_calls=tool_calls,
        )
        self._log_result(result)
        return result

    async def stream(
        self, request: CompletionRequest, on_delta: Callable[[str], Awaitable[None]]
    ) -> CompletionResult:
        started = perf_counter()
        arguments = self._arguments(request)
        self._log_arguments(arguments)
        response = await litellm.acompletion(**arguments, stream=True)
        text_parts: list[str] = []
        request_id: str | None = None
        finish_reason: str | None = None
        usage: Any = None
        tool_call_parts: dict[int, dict[str, str]] = {}
        async for chunk in response:
            request_id = getattr(chunk, "id", request_id)
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = chunk_usage
            choices = getattr(chunk, "choices", [])
            if not choices:
                continue
            choice = choices[0]
            finish_reason = getattr(choice, "finish_reason", None) or finish_reason
            choice_delta = getattr(choice, "delta", None)
            delta = getattr(choice_delta, "content", None) or ""
            if delta:
                text_parts.append(delta)
                await on_delta(delta)
            raw_tool_calls = getattr(choice_delta, "tool_calls", None) or []
            if raw_tool_calls:
                _merge_stream_tool_calls(tool_call_parts, raw_tool_calls)
                # Tool-call chunks contain no visible text but still count as stream
                # activity for the router's first-chunk/idle watchdog.
                await on_delta("")
        tool_calls = _tool_calls_from_parts(tool_call_parts)
        result = self._result(
            request=request,
            text="".join(text_parts),
            request_id=request_id,
            finish_reason=finish_reason,
            usage=usage,
            started=started,
            tool_calls=tool_calls,
        )
        self._log_result(result)
        return result

    def _arguments(self, request: CompletionRequest) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "model": f"custom_openai/{self.endpoint.model}",
            "api_base": str(self.endpoint.base_url).rstrip("/"),
            "messages": [_message_payload(message) for message in request.messages],
            # 思考型端点的推理 token 不占可见正文预算，线上额度按端点配置叠加
            "max_tokens": request.max_tokens + self.endpoint.reasoning_overhead_tokens,
            "temperature": request.temperature,
            "timeout": self.endpoint.timeout_ms / 1_000,
            "num_retries": 0,
        }
        if self.endpoint.secret_value is not None:
            arguments["api_key"] = self.endpoint.secret_value
        elif self.endpoint.secret_ref is not None:
            arguments["api_key"] = self._secrets.resolve(self.endpoint.secret_ref)
        elif self.endpoint.runs_local:
            # OpenAI-compatible clients require a non-empty api_key argument even
            # when a local runtime such as LM Studio has authentication disabled.
            # This fixed placeholder is not a credential and never leaves the
            # configured local endpoint.
            arguments["api_key"] = "local-no-auth"
        if request.json_mode and self.endpoint.supports_json_mode:
            arguments["response_format"] = {"type": "json_object"}
        if request.tools:
            arguments["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]
            arguments["tool_choice"] = request.tool_choice
        if self.endpoint.thinking_mode != "provider_default":
            arguments["extra_body"] = {
                "thinking": {"type": self.endpoint.thinking_mode},
            }
        return arguments

    def _result(
        self,
        *,
        request: CompletionRequest,
        text: str,
        request_id: str | None,
        finish_reason: str | None,
        usage: Any,
        started: float,
        tool_calls: list[ToolCall] | None = None,
    ) -> CompletionResult:
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        estimated_cost = (
            input_tokens * self.endpoint.input_cost_per_million
            + output_tokens * self.endpoint.output_cost_per_million
        ) / 1_000_000
        return CompletionResult(
            text=text,
            provider=self.endpoint.provider,
            model=self.endpoint.model,
            endpoint=self.endpoint_name,
            route=request.route,
            request_id=request_id,
            finish_reason=finish_reason,
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                estimated_cost=estimated_cost,
            ),
            latency_ms=(perf_counter() - started) * 1_000,
            tool_calls=tool_calls or [],
        )

    def _log_arguments(self, arguments: dict[str, Any]) -> None:
        safe = {k: v for k, v in arguments.items() if k != "api_key"}
        safe["model"] = self.endpoint.model
        logger.info(
            "【发起请求】llm request endpoint=%s model=%s arguments=%s",
            self.endpoint_name,
            self.endpoint.model,
            json.dumps(
                redact_fields(safe),
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            ),
        )

    def _log_result(self, result: CompletionResult) -> None:
        if result.finish_reason == "stop":
            note = "【正常完成】"
        elif result.finish_reason == "length":
            note = "【输出被截断, 建议调高 max_tokens】"
        else:
            note = f"【结束原因: {result.finish_reason}】"
        logger.info(
            "%s llm response endpoint=%s model=%s result=%s",
            note,
            self.endpoint_name,
            self.endpoint.model,
            json.dumps(
                redact_fields(result.model_dump(mode="json")),
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            ),
        )

    async def probe(self) -> None:
        if self.endpoint.supports_tool_calling:
            result = await self.complete(
                CompletionRequest(
                    trace_id=uuid7(),
                    messages=[
                        {
                            "role": "user",
                            "content": "Call the report_probe function now. Do not answer in text.",
                        }
                    ],
                    privacy_level="L0",
                    route="utility",
                    max_tokens=min(self.endpoint.max_tokens or 128, 1024),
                    temperature=0,
                    tools=[
                        ToolDefinition(
                            name="report_probe",
                            description="Synthetic Function Calling connectivity check.",
                            parameters={
                                "type": "object",
                                "properties": {},
                                "additionalProperties": False,
                            },
                        )
                    ],
                )
            )
            if len(result.tool_calls) != 1:
                raise RuntimeError("provider_probe_tool_call_missing")
            if result.tool_calls[0].function.name != "report_probe":
                raise RuntimeError("provider_probe_tool_call_invalid")
            return
        probe_prompt = (
            'Synthetic connectivity check. Reply only with {"ok":true}.'
            if self.endpoint.supports_json_mode
            else "Synthetic connectivity check. Reply only with OK."
        )
        result = await self.complete(
            CompletionRequest(
                trace_id=uuid7(),
                messages=[{"role": "user", "content": probe_prompt}],
                privacy_level="L0",
                route="utility",
                max_tokens=min(self.endpoint.max_tokens or 128, 1024),
                temperature=0,
                json_mode=True,
            )
        )
        if not result.text.strip():
            raise RuntimeError("provider_probe_empty_response")
        if self.endpoint.supports_json_mode:
            parsed = json.loads(result.text)
            if not isinstance(parsed, dict) or parsed.get("ok") is not True:
                raise RuntimeError("provider_probe_invalid_response")


def _message_payload(message: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    if message.name is not None:
        payload["name"] = message.name
    if message.tool_calls:
        # 协议要求 assistant.tool_calls[].function.arguments 是 JSON 字符串；
        # 内部契约为已解析的 dict，出站前必须重新编码，否则回注请求被 400 拒绝。
        payload["tool_calls"] = [
            {
                "id": item.id,
                "type": "function",
                "function": {
                    "name": item.function.name,
                    "arguments": json.dumps(
                        item.function.arguments,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            }
            for item in message.tool_calls
        ]
    return payload


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise ValueError("tool arguments must be a JSON object")
    parsed = json.loads(raw or "{}")
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must be a JSON object")
    return parsed


def _parse_tool_calls(raw_calls: Any) -> list[ToolCall]:
    result: list[ToolCall] = []
    for raw in raw_calls or []:
        function = _value(raw, "function")
        result.append(
            ToolCall(
                id=str(_value(raw, "id")),
                function={
                    "name": _value(function, "name"),
                    "arguments": _parse_arguments(_value(function, "arguments", "{}")),
                },
            )
        )
    return result


def _merge_stream_tool_calls(parts: dict[int, dict[str, str]], raw_calls: Any) -> None:
    for fallback_index, raw in enumerate(raw_calls):
        index = int(_value(raw, "index", fallback_index))
        current = parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
        call_id = _value(raw, "id")
        if call_id:
            current["id"] = str(call_id)
        function = _value(raw, "function")
        if function is None:
            continue
        name = _value(function, "name")
        arguments = _value(function, "arguments")
        if name:
            current["name"] += str(name)
        if arguments:
            current["arguments"] += str(arguments)


def _tool_calls_from_parts(parts: dict[int, dict[str, str]]) -> list[ToolCall]:
    return [
        ToolCall(
            id=value["id"],
            function={
                "name": value["name"],
                "arguments": _parse_arguments(value["arguments"]),
            },
        )
        for _, value in sorted(parts.items())
    ]
