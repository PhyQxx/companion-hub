"""Detached message extraction preserves candidate and chat request semantics."""

import asyncio
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from app.ids import uuid7
from app.llm.contracts import CompletionRequest, CompletionResult, LLMRoute
from app.memory.extraction import LlmMemoryExtractor
from app.memory.extraction_core import StructuredMemoryExtractor
from app.memory.extraction_ports import MemoryExtractionCompletion, MemoryExtractionInput
from app.memory.extraction_rules import (
    LLM_EXTRACTOR_VERSION,
    MAX_EXTRACT_INPUT_CHARS,
    RULE_EXTRACTOR_VERSION,
    RuleBasedExtractor,
    extraction_instruction,
)
from app.memory.models import MemoryCandidate
from app.schemas.common import PrivacyLevel
from scripts.check_architecture import allowed

NOW = datetime(2030, 1, 1, tzinfo=UTC)
VALID = json.dumps(
    {
        "candidates": [
            {
                "type": "semantic",
                "content": "用户职业是软件工程师",
                "importance": 0.8,
                "confidence": 0.9,
            }
        ]
    }
)
TEXT = "我喜欢吃合成测试水果。"


class Completion:
    def __init__(self, text: str = VALID, error: BaseException | None = None) -> None:
        self.text, self.error = text, error
        self.inputs: list[MemoryExtractionInput] = []

    async def complete(self, request: MemoryExtractionInput) -> MemoryExtractionCompletion:
        self.inputs.append(request)
        if self.error is not None:
            raise self.error
        return MemoryExtractionCompletion(self.text)


async def extracted(
    completion: Completion | None, privacy: PrivacyLevel = PrivacyLevel.L1
) -> list[MemoryCandidate]:
    return await StructuredMemoryExtractor().extract(
        TEXT, message_id=uuid7(), privacy_level=privacy, occurred_at=NOW, completion=completion
    )


@pytest.mark.parametrize("privacy", [PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2])
async def test_memory_completion_maps_source_version_and_privacy(privacy: PrivacyLevel) -> None:
    completion = Completion()
    result = await extracted(completion, privacy)
    candidate = result[0]
    assert candidate.fact_key == "profile.job" and candidate.privacy_level == privacy
    assert candidate.extractor_version == LLM_EXTRACTOR_VERSION and candidate.valid_from == NOW
    assert candidate.sources[0].source_id == str(completion.inputs[0].message_id)
    assert candidate.sources[0].source_kind == "message" and candidate.sources[0].excerpt is None
    assert completion.inputs[0].privacy_level is privacy
    assert ("隐私约束" in completion.inputs[0].instruction) == (privacy is PrivacyLevel.L2)


async def test_l3_memory_skips_completion_and_rule_fallback() -> None:
    completion = Completion()
    assert await extracted(completion, PrivacyLevel.L3) == []
    assert completion.inputs == []


@pytest.mark.parametrize("privacy", list(PrivacyLevel))
async def test_missing_completion_preserves_rule_privacy(privacy: PrivacyLevel) -> None:
    result = await extracted(None, privacy)
    if privacy in {PrivacyLevel.L2, PrivacyLevel.L3}:
        assert result == []
    else:
        assert len(result) == 1 and result[0].extractor_version == RULE_EXTRACTOR_VERSION


@pytest.mark.parametrize(
    "bad",
    [
        "bad",
        "[]",
        "null",
        '{"candidates":[{"type":"unknown","content":"Synthetic"}]}',
        '{"candidates":[{"type":"semantic","content":""}]}',
        '{"candidates":[{"type":"semantic","content":"Synthetic","confidence":2}]}',
        '{"candidates":[{"type":"semantic","content":"Synthetic","confidence":-1}]}',
        '{"candidates":[{"type":"semantic","content":"Synthetic","confidence":Infinity}]}',
        '{"candidates":[{"type":"semantic","content":"Synthetic","importance":NaN}]}',
        '{"candidates":[{"type":"semantic","content":"Synthetic","importance":Infinity}]}',
    ],
)
async def test_invalid_memory_payload_keeps_rule_fallback(bad: str) -> None:
    result = await extracted(Completion(bad))
    assert len(result) == 1 and result[0].extractor_version == RULE_EXTRACTOR_VERSION


async def test_memory_completion_error_keeps_rule_fallback() -> None:
    result = await extracted(Completion(error=RuntimeError("synthetic failure")))
    assert len(result) == 1 and result[0].extractor_version == RULE_EXTRACTOR_VERSION


@pytest.mark.parametrize("importance", [-2, 2])
async def test_finite_memory_importance_keeps_original_clipping(importance: float) -> None:
    completion = Completion(
        json.dumps(
            {"candidates": [{"type": "semantic", "content": "Synthetic", "importance": importance}]}
        )
    )
    result = await extracted(completion)
    assert result[0].importance == (0 if importance < 0 else 1)
    assert result[0].extractor_version == LLM_EXTRACTOR_VERSION


@pytest.mark.parametrize("confidence", [None, 0, 1])
async def test_memory_confidence_accepts_unknown_and_probability_boundaries(
    confidence: float | None,
) -> None:
    completion = Completion(
        json.dumps(
            {"candidates": [{"type": "semantic", "content": "Synthetic", "confidence": confidence}]}
        )
    )
    result = await extracted(completion)
    assert (
        result[0].confidence == confidence and result[0].extractor_version == LLM_EXTRACTOR_VERSION
    )


async def test_memory_cancellation_propagates() -> None:
    with pytest.raises(asyncio.CancelledError):
        await extracted(Completion(error=asyncio.CancelledError()))


@pytest.mark.parametrize(
    "framing", ["{}", "```json\n{}\n```", "synthetic prefix\n{}\nsynthetic suffix"]
)
async def test_memory_payload_framing_is_preserved(framing: str) -> None:
    result = await extracted(Completion(framing.format(VALID)))
    assert result[0].extractor_version == LLM_EXTRACTOR_VERSION


async def test_empty_memory_completion_does_not_invent_rule_candidates() -> None:
    assert await extracted(Completion('{"candidates":[]}')) == []


async def test_memory_completion_clips_input_and_keeps_four_candidates() -> None:
    completion = Completion(
        json.dumps(
            {
                "candidates": [
                    {"type": "semantic", "content": f"Synthetic {index}"} for index in range(6)
                ]
            }
        )
    )
    result = await StructuredMemoryExtractor().extract(
        "x" * 5000,
        message_id=uuid7(),
        privacy_level=PrivacyLevel.L1,
        occurred_at=NOW,
        completion=completion,
    )
    assert len(completion.inputs[0].text) == MAX_EXTRACT_INPUT_CHARS
    assert [candidate.content for candidate in result] == [
        f"Synthetic {index}" for index in range(4)
    ]


def test_memory_completion_dtos_are_frozen() -> None:
    def overwrite(value: object, attribute: str) -> None:
        setattr(value, attribute, "changed")

    with pytest.raises(FrozenInstanceError):
        overwrite(MemoryExtractionInput(uuid7(), "instruction", "text", PrivacyLevel.L1), "text")
    with pytest.raises(FrozenInstanceError):
        overwrite(MemoryExtractionCompletion("text"), "text")


@pytest.mark.parametrize("privacy", [PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2])
async def test_legacy_memory_adapter_retains_request_contract(privacy: PrivacyLevel) -> None:
    requests: list[CompletionRequest] = []

    class Backend:
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            requests.append(request)
            return CompletionResult(
                text=VALID,
                provider="fixture",
                model="fixture",
                endpoint="fixture",
                route=request.route,
                latency_ms=0,
            )

    identifier = uuid7()
    result = await LlmMemoryExtractor().extract(
        TEXT, message_id=identifier, privacy_level=privacy, occurred_at=NOW, backend=Backend()
    )
    assert result[0].extractor_version == LLM_EXTRACTOR_VERSION
    request = requests[0]
    assert request.trace_id == identifier and request.privacy_level == privacy
    assert request.route == LLMRoute.UTILITY and request.json_mode and request.temperature == 0.1
    assert request.max_tokens == 1024 and len(request.messages) == 2
    assert request.messages[0].content == extraction_instruction(privacy)
    assert request.messages[1].content == TEXT


def test_memory_rule_and_json_legacy_exports_are_compatible() -> None:
    from app.memory import RuleBasedExtractor as package_rule
    from app.memory.extraction import RuleBasedExtractor as legacy_rule
    from app.memory.extraction import _load_json_object

    assert package_rule is legacy_rule is RuleBasedExtractor
    assert _load_json_object('```json\n{"candidates":[]}\n```') == {"candidates": []}


@pytest.mark.parametrize(
    "module",
    ["app.memory.extraction_core", "app.memory.extraction_ports", "app.memory.extraction_rules"],
)
@pytest.mark.parametrize(
    "adapter", ["app.db", "app.llm", "app.memory.store", "sqlalchemy", "httpx"]
)
def test_memory_extraction_core_cannot_import_runtime_adapters(module: str, adapter: str) -> None:
    assert not allowed(module, adapter)
