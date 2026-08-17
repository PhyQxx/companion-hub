from __future__ import annotations

import json
import os
from collections.abc import Mapping
from time import perf_counter
from typing import Any, Protocol

import litellm

from app.ids import uuid7

from .contracts import CompletionRequest, CompletionResult, ModelEndpoint, ModelUsage


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
        arguments: dict[str, Any] = {
            "model": f"openai/{self.endpoint.model}",
            "api_base": str(self.endpoint.base_url).rstrip("/"),
            "messages": [message.model_dump(mode="json") for message in request.messages],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "timeout": self.endpoint.timeout_ms / 1_000,
            "num_retries": 0,
        }
        if self.endpoint.secret_ref is not None:
            arguments["api_key"] = self._secrets.resolve(self.endpoint.secret_ref)
        if request.json_mode and self.endpoint.supports_json_mode:
            arguments["response_format"] = {"type": "json_object"}
        response = await litellm.acompletion(**arguments)
        choice = response.choices[0]
        content = choice.message.content or ""
        usage = response.usage
        input_tokens = int(usage.prompt_tokens or 0) if usage is not None else 0
        output_tokens = int(usage.completion_tokens or 0) if usage is not None else 0
        estimated_cost = (
            input_tokens * self.endpoint.input_cost_per_million
            + output_tokens * self.endpoint.output_cost_per_million
        ) / 1_000_000
        return CompletionResult(
            text=content,
            provider=self.endpoint.provider,
            model=self.endpoint.model,
            endpoint=self.endpoint_name,
            route=request.route,
            request_id=response.id,
            finish_reason=choice.finish_reason,
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                estimated_cost=estimated_cost,
            ),
            latency_ms=(perf_counter() - started) * 1_000,
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
                max_tokens=128,
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
