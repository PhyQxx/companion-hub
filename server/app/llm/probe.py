"""Synthetic connection request and response criteria without SDK or SQL."""

import json
from typing import Protocol

from app.ids import uuid7

from .contracts import CompletionRequest, CompletionResult, ModelEndpoint, ToolDefinition


class ProbeInference(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResult: ...


def probe_request(endpoint: ModelEndpoint) -> CompletionRequest:
    if endpoint.supports_tool_calling:
        return CompletionRequest(
            trace_id=uuid7(),
            messages=[
                {
                    "role": "user",
                    "content": "Call the report_probe function now. Do not answer in text.",
                }
            ],
            privacy_level="L0",
            route="utility",
            max_tokens=min(endpoint.max_tokens or 128, 1024),
            temperature=0,
            tools=[
                ToolDefinition(
                    name="report_probe",
                    description="Synthetic Function Calling connectivity check.",
                    parameters={"type": "object", "properties": {}, "additionalProperties": False},
                )
            ],
        )
    return CompletionRequest(
        trace_id=uuid7(),
        messages=[
            {
                "role": "user",
                "content": (
                    'Synthetic connectivity check. Reply only with {"ok":true}.'
                    if endpoint.supports_json_mode
                    else "Synthetic connectivity check. Reply only with OK."
                ),
            }
        ],
        privacy_level="L0",
        route="utility",
        max_tokens=min(endpoint.max_tokens or 128, 1024),
        temperature=0,
        json_mode=True,
    )


def validate_probe(result: CompletionResult, endpoint: ModelEndpoint) -> None:
    if endpoint.supports_tool_calling:
        if len(result.tool_calls) != 1:
            raise RuntimeError("provider_probe_tool_call_missing")
        if result.tool_calls[0].function.name != "report_probe":
            raise RuntimeError("provider_probe_tool_call_invalid")
        return
    if not result.text.strip():
        raise RuntimeError("provider_probe_empty_response")
    if endpoint.supports_json_mode:
        parsed = json.loads(result.text)
        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            raise RuntimeError("provider_probe_invalid_response")
