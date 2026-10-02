"""Synthetic local counting fixtures; no provider, secrets or execution port."""

import hashlib
import json
from importlib.metadata import version
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from app.llm.contracts import (
    CompletionRequest,
    ContextTokenizer,
    LLMMessage,
    ModelEndpoint,
    ToolDefinition,
)
from app.schemas.common import StrictModel, TokenName

from .window import fit_window


class CalibrationCase(StrictModel):
    id: TokenName
    messages: Annotated[list[LLMMessage], Field(min_length=1, max_length=256)]
    tools: Annotated[list[ToolDefinition], Field(max_length=32)] = Field(default_factory=list)
    prompt_tokens: Annotated[int, Field(ge=0, le=1_000_000_000)]
    usage_known: Literal[True]


class CalibrationCorpus(StrictModel):
    version: Literal[1]
    synthetic: Literal[True]
    provider: TokenName
    model: Annotated[str, Field(min_length=1, max_length=200)]
    cases: Annotated[list[CalibrationCase], Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def unique_cases(self) -> "CalibrationCorpus":
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("calibration_case_ids_not_unique")
        return self


def calibrate(corpus: CalibrationCorpus, spec: ContextTokenizer) -> dict[str, object]:
    endpoint = ModelEndpoint(
        provider=corpus.provider,
        model=corpus.model,
        base_url="http://127.0.0.1:1",
        runs_local=True,
        max_privacy_level="L2",
        max_context_tokens=1_000_000_000,
        context_tokenizer=spec,
    )
    results = []
    largest_gap = 0
    for case in corpus.cases:
        fitted = fit_window(
            CompletionRequest(
                trace_id=uuid4(),
                messages=case.messages,
                tools=case.tools,
                privacy_level="L2",
                route="private",
            ),
            endpoint,
        )
        estimate = int(fitted.manifest["estimated_input_tokens"]) + spec.protocol_reserve_tokens
        gap = max(0, case.prompt_tokens - estimate)
        largest_gap = max(largest_gap, gap)
        results.append(
            {
                "id": case.id,
                "estimated_input_with_protocol": estimate,
                "declared_prompt_tokens": case.prompt_tokens,
                "underestimate_tokens": gap,
            }
        )
    canonical = json.dumps(
        corpus.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    return {
        "version": 1,
        "validation_level": "V2",
        "semantic_validation": "unverified",
        "usage_provenance": "operator_declared_synthetic_fixture",
        "provider_protocol_validation": "unverified",
        "provider": corpus.provider,
        "model": corpus.model,
        "corpus_sha256": hashlib.sha256(canonical).hexdigest(),
        "tokenizer_id": spec.id,
        "tokenizer_sha256": spec.sha256,
        "tokenizer_library_version": version("tokenizers"),
        "estimator": "native_json_estimate_v1",
        "safety_multiplier": spec.safety_multiplier,
        "protocol_reserve_tokens": spec.protocol_reserve_tokens,
        "all_cases_within_estimate": largest_gap == 0,
        "max_underestimate_tokens": largest_gap,
        "suggested_protocol_reserve_for_this_corpus": spec.protocol_reserve_tokens + largest_gap,
        "cases": results,
    }
