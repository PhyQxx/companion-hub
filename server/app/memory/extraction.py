# ruff: noqa: RUF001
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Protocol, final
from uuid import UUID

from app.llm import CompletionRequest, CompletionResult, LLMMessage, LLMRoute
from app.schemas.common import PrivacyLevel

from .models import (
    ExtractedCandidates,
    MemoryCandidate,
    MemorySourceKind,
    MemorySourceRef,
    MemoryType,
)

RULE_EXTRACTOR_VERSION = "rule-v1"
LLM_EXTRACTOR_VERSION = "llm-utility-v1"

MAX_EXTRACT_INPUT_CHARS = 4_000
MAX_LLM_CANDIDATES = 4

_SENTENCE_SPLIT = re.compile(r"[。！？!?\n；;]+")
_TIME_WORDS = re.compile(
    r"(明天|后天|今天|下周|本周|下个月|月底|周[一二三四五六日天]|\d{1,2}月\d{1,2}[号日]|\d{1,2}号)"
)
_COMMITMENT_MODAL = re.compile(r"(要|得|会|准备|打算|想去|要去|得去|将)")
_PREFERENCE_PATTERNS = (
    re.compile(r"我(不吃|不喝|不爱吃|不喜欢吃|讨厌吃|忌口?)"),
    re.compile(r"我(最喜欢?|爱|偏好|喜欢)"),
    re.compile(r"我(不喜欢|不爱|讨厌|不想)"),
)
_SEMANTIC_PATTERN = re.compile(r"我(是|姓|叫|住在|家在|从事|工作是|在.{1,12}(工作|上学|生活))")
_PERSONA_DIRECTED = re.compile(r"[你妳]")


class ExtractionBackend(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResult: ...


class MemoryExtractor(Protocol):
    async def extract(
        self,
        text: str,
        *,
        message_id: UUID,
        privacy_level: PrivacyLevel,
        occurred_at: datetime,
        backend: ExtractionBackend | None = None,
    ) -> list[MemoryCandidate]: ...


def extraction_instruction(privacy_level: PrivacyLevel) -> str:
    instruction = (
        "你是长期记忆提取器。从用户消息中提取值得长期记住的内容，只输出严格 JSON：\n"
        '{"candidates":[{"type":"semantic|preference|commitment|episodic|emotional",'
        '"content":"……","importance":0.6,"confidence":0.8}]}\n'
        "要求：content 用第三人称“用户……”的中文描述，不超过 80 字；"
        "只提取稳定事实、偏好、承诺或重要事件；寒暄和与用户无关的内容不要提取；"
        "没有值得记忆的内容时输出 {\"candidates\":[]}。"
    )
    if privacy_level is PrivacyLevel.L2:
        instruction += (
            "\n隐私约束：这是亲密对话，content 只能是事件级概括"
            "（发生了什么 / 时长 / 用户情绪倾向），严禁任何生理、身体或露骨细节。"
        )
    return instruction


@final
class LlmMemoryExtractor:
    """Utility-model extraction with the deterministic rule extractor as fallback.

    Any model failure — network, malformed JSON, schema violation — degrades to
    the rule extractor instead of losing the turn's consolidation entirely.
    """

    def __init__(self, *, fallback: RuleBasedExtractor | None = None) -> None:
        self._fallback = fallback or RuleBasedExtractor()

    async def extract(
        self,
        text: str,
        *,
        message_id: UUID,
        privacy_level: PrivacyLevel,
        occurred_at: datetime,
        backend: ExtractionBackend | None = None,
    ) -> list[MemoryCandidate]:
        if privacy_level is PrivacyLevel.L3:
            return []
        if backend is None:
            return await self._fallback.extract(
                text,
                message_id=message_id,
                privacy_level=privacy_level,
                occurred_at=occurred_at,
            )
        request = CompletionRequest(
            trace_id=message_id,
            messages=[
                LLMMessage(role="system", content=extraction_instruction(privacy_level)),
                LLMMessage(role="user", content=text[:MAX_EXTRACT_INPUT_CHARS]),
            ],
            privacy_level=privacy_level,
            route=LLMRoute.UTILITY,
            max_tokens=512,
            temperature=0.1,
            json_mode=True,
        )
        try:
            result = await backend.complete(request)
            payload = ExtractedCandidates.model_validate(_load_json_object(result.text))
        except Exception:
            return await self._fallback.extract(
                text,
                message_id=message_id,
                privacy_level=privacy_level,
                occurred_at=occurred_at,
            )
        return [
            MemoryCandidate(
                type=item.type,
                content=item.content,
                privacy_level=privacy_level,
                sources=[
                    MemorySourceRef(
                        source_kind=MemorySourceKind.MESSAGE,
                        source_id=str(message_id),
                    )
                ],
                importance=min(1.0, max(0.0, item.importance)),
                confidence=item.confidence,
                extractor_version=LLM_EXTRACTOR_VERSION,
                valid_from=occurred_at,
            )
            for item in payload.candidates[:MAX_LLM_CANDIDATES]
        ]


@final
class RuleBasedExtractor:
    """Conservative deterministic extractor for first-person stable statements.

    L2/L3 conversations are skipped: without the desensitizing utility model
    they must not persist anything.
    """

    async def extract(
        self,
        text: str,
        *,
        message_id: UUID,
        privacy_level: PrivacyLevel,
        occurred_at: datetime,
        backend: ExtractionBackend | None = None,
    ) -> list[MemoryCandidate]:
        if privacy_level in {PrivacyLevel.L2, PrivacyLevel.L3}:
            return []
        results: list[MemoryCandidate] = []
        seen: set[str] = set()
        for raw_sentence in _SENTENCE_SPLIT.split(text):
            sentence = raw_sentence.strip()
            if not (2 <= len(sentence) <= 200):
                continue
            first_person = sentence.find("我")
            if first_person < 0:
                continue
            clause = sentence[first_person:]
            memory_type = _classify(clause)
            if memory_type is None or clause in seen:
                continue
            seen.add(clause)
            results.append(
                MemoryCandidate(
                    type=memory_type,
                    content=_third_person(clause),
                    privacy_level=PrivacyLevel.L1,
                    sources=[
                        MemorySourceRef(
                            source_kind=MemorySourceKind.MESSAGE,
                            source_id=str(message_id),
                            excerpt=clause,
                        )
                    ],
                    importance=_default_importance(memory_type),
                    confidence=0.6,
                    extractor_version=RULE_EXTRACTOR_VERSION,
                    valid_from=occurred_at,
                    valid_to=(
                        occurred_at + timedelta(days=7)
                        if memory_type is MemoryType.COMMITMENT
                        else None
                    ),
                )
            )
        return results[:4]


def _load_json_object(raw: str) -> dict[str, object]:
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped.split("\n", 1)[-1]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in extractor output")
    payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("extractor output is not a JSON object")
    return payload


def _classify(sentence: str) -> MemoryType | None:
    if not sentence.startswith("我"):
        return None
    if _TIME_WORDS.search(sentence) and _COMMITMENT_MODAL.search(sentence):
        return MemoryType.COMMITMENT
    if any(pattern.search(sentence) for pattern in _PREFERENCE_PATTERNS):
        return None if _PERSONA_DIRECTED.search(sentence) else MemoryType.PREFERENCE
    if _SEMANTIC_PATTERN.search(sentence):
        return None if _PERSONA_DIRECTED.search(sentence) else MemoryType.SEMANTIC
    return None


def _third_person(sentence: str) -> str:
    if sentence.startswith("我"):
        return f"用户{sentence[1:]}"
    return sentence


def _default_importance(memory_type: MemoryType) -> float:
    if memory_type is MemoryType.COMMITMENT:
        return 0.7
    if memory_type in {MemoryType.PREFERENCE, MemoryType.SEMANTIC}:
        return 0.6
    return 0.5
