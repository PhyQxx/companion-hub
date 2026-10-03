# ruff: noqa: RUF001
"""Pure message/turn extraction rules and candidate helpers."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from app.harness.json_payload import load_json_object
from app.schemas.common import PrivacyLevel

from .models import (
    MemoryCandidate,
    MemoryEntry,
    MemoryOriginKind,
    MemorySourceKind,
    MemorySourceRef,
    MemorySubjectKind,
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
_USER_JOB_FACT = re.compile(
    r"(工作(?:是|为)|职业(?:是|为)|从事|从业者|"
    r"(?:是|担任)[^，。！？!?\n；;]{0,24}"
    r"(?:工程师|程序员|产品负责人|产品经理|设计师|教师|医生))"
)
_PERSONA_DIRECTED = re.compile(r"[你妳]")
_UNCERTAIN_SELF = re.compile(r"(也许|可能|大概|或许|如果|假如|好像|差不多|左右|不确定)")
_MEASUREMENTS_PATTERN = re.compile(
    r"(?:我的)?三围(?:是|为|[:：])?\s*(\d{2,3})\s*[-—–/－]\s*(\d{2,3})"
    r"\s*[-—–/－]\s*(\d{2,3})"
)
_HEIGHT_PATTERN = re.compile(
    r"(?:我的|你的)?身高(?:是|为|[:：])?\s*(\d{2,3}(?:\.\d+)?)\s*(?:cm|厘米|公分)?",
    re.IGNORECASE,
)
# 口语里常省略“身高”二字（“我165厘米”“你170cm”），此时必须有单位锚定，
# 否则“我买了个165厘米的柜子”这类句子会误入档案
_HEIGHT_VALUE_PATTERN = re.compile(
    r"(?:我|你)[的啊呀]?(?:身高)?(?:大概|大约|差不多)?[是为一有]?[\s，,]*"
    r"(\d{2,3}(?:\.\d+)?)\s*(?:cm|厘米|公分)",
    re.IGNORECASE,
)
_WEIGHT_PATTERN = re.compile(
    r"(?:我的|你的)?体重(?:是|为|[:：])?\s*(\d{2,3}(?:\.\d+)?)\s*(?:kg|公斤|千克)?",
    re.IGNORECASE,
)
_WEIGHT_VALUE_PATTERN = re.compile(
    r"(?:我|你)[的啊呀]?(?:体重)?(?:大概|大约|差不多)?[是为一有]?[\s，,]*"
    r"(\d{2,3}(?:\.\d+)?)\s*(?:kg|公斤|千克)",
    re.IGNORECASE,
)
_BIRTHDAY_PATTERN = re.compile(r"(?:我的|你的)?生日(?:是|为|[:：])\s*([^，。！？!?\n；;]{2,24})")
_SELF_NAME_PATTERN = re.compile(r"(?:我叫|我的名字是)\s*([^，。！？!?\n；;]{1,24})")
_USER_SET_NAME_PATTERN = re.compile(
    r"(?:记住[，,:：\s]*)?(?:你叫|你的名字是)\s*([^，。！？!?\n；;]{1,24})"
)
_NICKNAME_PATTERN = re.compile(
    r"(?:你(?:以后)?可以叫我|以后叫我|叫我)\s*([^，。！？!?\n；;]{1,24})"
)
_SELF_PREFERENCE_PATTERN = re.compile(
    r"我(?:很|最)?喜欢(?P<verb>吃|喝)?\s*(?P<value>[^，。！？!?\n；;]{1,48})"
)
_SHARED_MARKER = re.compile(r"(我们|咱们|咱俩|我们俩)")
_SHARED_COMMITMENT = re.compile(
    r"(约好|约定|以后|下次|明天|后天|今晚|周末|周[一二三四五六日天]).*"
    r"(一起|要|去|看|做|聊|玩|吃|喝)"
)


def extract_assistant_fact_assertions(text: str) -> list[tuple[str, str]]:
    """提取助手回复里明确声明的稳定槽位，供一致性 Guard 复用。"""

    assertions: list[tuple[str, str]] = []
    for raw_sentence in _SENTENCE_SPLIT.split(text):
        sentence = raw_sentence.strip()
        if not sentence or _UNCERTAIN_SELF.search(sentence):
            continue
        for _, fact_key, content in _assistant_facts_from_sentence(sentence, user_directed=False):
            if fact_key is not None:
                assertions.append((fact_key, content))
    return assertions


def extraction_instruction(privacy_level: PrivacyLevel) -> str:
    """构造提取提示词；L2 附加事件级脱敏约束（禁止生理/身体细节）。"""
    instruction = (
        "你是长期记忆提取器。从用户消息中提取值得长期记住的内容，只输出严格 JSON：\n"
        '{"candidates":[{"type":"semantic|preference|commitment|episodic|emotional",'
        '"content":"……","importance":0.6,"confidence":0.8}]}\n'
        "要求：content 用第三人称“用户……”的中文描述，不超过 80 字；"
        "只提取稳定事实、偏好、承诺或重要事件；寒暄和与用户无关的内容不要提取；"
        '没有值得记忆的内容时输出 {"candidates":[]}。'
    )
    if privacy_level == PrivacyLevel.L2:
        instruction += (
            "\n隐私约束：这是亲密对话，content 只能是事件级概括"
            "（发生了什么 / 时长 / 用户情绪倾向），严禁任何生理、身体或露骨细节。"
        )
    return instruction


class RuleBasedExtractor:
    """保守的确定性提取器：只识别第一人称稳定陈述。

    L2/L3 会话一律跳过——没有脱敏能力的内容不允许落库；L2 的脱敏
    沉淀由 LlmMemoryExtractor 承担。
    """

    async def extract(
        self,
        text: str,
        *,
        message_id: UUID,
        privacy_level: PrivacyLevel,
        occurred_at: datetime,
        backend: object | None = None,
    ) -> list[MemoryCandidate]:
        # 规则路径没有脱敏能力：L2/L3 不产生任何候选
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
                    fact_key=_infer_user_fact_key(clause),
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
    """Keep the legacy extraction helper available to existing callers."""
    return load_json_object(raw)


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


def _infer_user_fact_key(content: str) -> str | None:
    """为稳定用户事实补确定性槽位，避免 LLM 分类波动破坏精确召回。"""

    return "profile.job" if _USER_JOB_FACT.search(content) else None


def _default_importance(memory_type: MemoryType) -> float:
    if memory_type is MemoryType.COMMITMENT:
        return 0.7
    if memory_type in {MemoryType.PREFERENCE, MemoryType.SEMANTIC}:
        return 0.6
    return 0.5


def _extract_user_directed_candidates(
    text: str, *, message_id: UUID, occurred_at: datetime
) -> list[MemoryCandidate]:
    """提取用户明确赋予助手的事实，以及有用户证据的 shared 约定。"""

    results: list[MemoryCandidate] = []
    source = MemorySourceRef(
        source_kind=MemorySourceKind.MESSAGE,
        source_id=str(message_id),
    )
    for raw_sentence in _SENTENCE_SPLIT.split(text):
        sentence = raw_sentence.strip()
        if not sentence:
            continue
        for memory_type, fact_key, content in _assistant_facts_from_sentence(
            sentence, user_directed=True
        ):
            results.append(
                MemoryCandidate(
                    subject_kind=MemorySubjectKind.ASSISTANT,
                    subject_key="assistant:primary",
                    fact_key=fact_key,
                    origin_kind=MemoryOriginKind.USER_STATEMENT,
                    type=memory_type,
                    content=content,
                    privacy_level=PrivacyLevel.L1,
                    sources=[source],
                    importance=0.75,
                    confidence=0.9,
                    extractor_version="turn-rule-v1",
                    valid_from=occurred_at,
                )
            )
        if _SHARED_MARKER.search(sentence) and _SHARED_COMMITMENT.search(sentence):
            results.append(
                MemoryCandidate(
                    subject_kind=MemorySubjectKind.SHARED,
                    subject_key="shared:user-assistant",
                    origin_kind=MemoryOriginKind.SHARED_TURN,
                    type=MemoryType.COMMITMENT,
                    content=f"双方约定：{sentence}",
                    privacy_level=PrivacyLevel.L1,
                    sources=[source],
                    importance=0.7,
                    confidence=0.8,
                    extractor_version="turn-rule-v1",
                    valid_from=occurred_at,
                )
            )
    return results


def _extract_assistant_candidates(
    text: str, *, message_id: UUID, occurred_at: datetime
) -> list[MemoryCandidate]:
    """只提取明确、稳定的助手自述；不把舞台动作和不确定说法长期化。"""

    results: list[MemoryCandidate] = []
    source = MemorySourceRef(
        source_kind=MemorySourceKind.MESSAGE,
        source_id=str(message_id),
    )
    for raw_sentence in _SENTENCE_SPLIT.split(text):
        sentence = raw_sentence.strip()
        if not sentence or _UNCERTAIN_SELF.search(sentence):
            continue
        for memory_type, fact_key, content in _assistant_facts_from_sentence(
            sentence, user_directed=False
        ):
            results.append(
                MemoryCandidate(
                    subject_kind=MemorySubjectKind.ASSISTANT,
                    subject_key="assistant:primary",
                    fact_key=fact_key,
                    origin_kind=MemoryOriginKind.ASSISTANT_STATEMENT,
                    type=memory_type,
                    content=content,
                    privacy_level=PrivacyLevel.L1,
                    sources=[source],
                    importance=0.75 if memory_type is MemoryType.SEMANTIC else 0.65,
                    confidence=0.9,
                    extractor_version="turn-rule-v1",
                    valid_from=occurred_at,
                )
            )
    return results


def _assistant_facts_from_sentence(
    sentence: str, *, user_directed: bool
) -> list[tuple[MemoryType, str | None, str]]:
    """把一句话中的多个助手稳定属性全部归一为 canonical content。"""

    if user_directed and not re.search(r"(你|你的)", sentence):
        return []
    if not user_directed and not re.search(r"(我|我的|叫我)", sentence):
        return []

    results: list[tuple[MemoryType, str | None, str]] = []

    measurements = _MEASUREMENTS_PATTERN.search(sentence)
    if measurements:
        value = "-".join(measurements.groups())
        results.append((MemoryType.SEMANTIC, "profile.measurements", f"助手三围为 {value} 厘米"))

    height = _HEIGHT_PATTERN.search(sentence) or _HEIGHT_VALUE_PATTERN.search(sentence)
    if height:
        results.append(
            (MemoryType.SEMANTIC, "profile.height", f"助手身高为 {height.group(1)} 厘米")
        )

    weight = _WEIGHT_PATTERN.search(sentence) or _WEIGHT_VALUE_PATTERN.search(sentence)
    if weight:
        results.append(
            (MemoryType.SEMANTIC, "profile.weight", f"助手体重为 {weight.group(1)} 公斤")
        )

    birthday = _BIRTHDAY_PATTERN.search(sentence)
    if birthday:
        value = birthday.group(1).strip()
        results.append((MemoryType.SEMANTIC, "profile.birthday", f"助手生日为 {value}"))

    if user_directed:
        name = _USER_SET_NAME_PATTERN.search(sentence)
    else:
        name = _SELF_NAME_PATTERN.search(sentence)
    if name:
        value = name.group(1).strip()
        results.append((MemoryType.SEMANTIC, "profile.name", f"助手名字为 {value}"))

    if not user_directed:
        nickname = _NICKNAME_PATTERN.search(sentence)
        if nickname:
            value = nickname.group(1).strip()
            results.append((MemoryType.SEMANTIC, "profile.nickname", f"助手昵称为 {value}"))

        preference = _SELF_PREFERENCE_PATTERN.search(sentence)
        if preference:
            value = preference.group("value").strip()
            if value.startswith(("你", "这样", "这么")):
                return results
            verb = preference.group("verb")
            fact_key = None
            if verb == "吃" or any(
                token in value for token in ("甜点", "蛋糕", "糕", "菜", "食物")
            ):
                fact_key = "preference.food"
            elif verb == "喝" or any(token in value for token in ("茶", "咖啡", "饮料", "果汁")):
                fact_key = "preference.drink"
            results.append((MemoryType.PREFERENCE, fact_key, f"助手喜欢{value}"))
    return results


def _dedupe_and_suppress_echo(
    candidates: Sequence[MemoryCandidate], retrieved_memories: Sequence[MemoryEntry]
) -> list[MemoryCandidate]:
    """去重，并阻止“记忆注入 → 助手复述 → 再当新证据”的回声链。"""

    retrieved_assistant = [
        memory
        for memory in retrieved_memories
        if memory.subject_kind == MemorySubjectKind.ASSISTANT.value
        and memory.subject_key == "assistant:primary"
    ]
    seen: set[tuple[str, str, str | None, str, str]] = set()
    results: list[MemoryCandidate] = []
    for candidate in candidates:
        subject = MemorySubjectKind(candidate.subject_kind).value
        memory_type = MemoryType(candidate.type).value
        key = (
            subject,
            candidate.subject_key,
            candidate.fact_key,
            memory_type,
            _normalize_memory_text(candidate.content),
        )
        if key in seen:
            continue
        seen.add(key)
        if subject == MemorySubjectKind.ASSISTANT.value and any(
            _normalize_memory_text(memory.content) == key[-1] for memory in retrieved_assistant
        ):
            continue
        results.append(candidate)
    return results


def _normalize_memory_text(text: str) -> str:
    return re.sub(r"[\s，。！？!?、：:；;（）()\-—–]+", "", text).lower()
