# ruff: noqa: RUF003
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

from .contracts import CompletionRequest, CompletionResult, ModelEndpoint, ModelUsage

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
        result = self._result(
            request=request,
            text=content,
            request_id=response.id,
            finish_reason=choice.finish_reason,
            usage=response.usage,
            started=started,
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
            delta = getattr(getattr(choice, "delta", None), "content", None) or ""
            if delta:
                text_parts.append(delta)
                await on_delta(delta)
        result = self._result(
            request=request,
            text="".join(text_parts),
            request_id=request_id,
            finish_reason=finish_reason,
            usage=usage,
            started=started,
        )
        self._log_result(result)
        return result

    def _arguments(self, request: CompletionRequest) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "model": f"custom_openai/{self.endpoint.model}",
            "api_base": str(self.endpoint.base_url).rstrip("/"),
            "messages": [message.model_dump(mode="json") for message in request.messages],
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
        )

    def _log_arguments(self, arguments: dict[str, Any]) -> None:
        safe = {k: v for k, v in arguments.items() if k != "api_key"}
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
            note = "【输出被截断，建议调高 max_tokens】"
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
