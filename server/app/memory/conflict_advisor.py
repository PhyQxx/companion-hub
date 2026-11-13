"""记忆冲突裁决顾问（方案 C4）：AI 给出裁决建议，人始终做最终决定。

合同：
- 仅建议、不执行——本模块不写库、不改状态，adopt/keep 仍走既有
  /resolve 人工确认路径（管理页二次确认框）；
- 模型输出不可解析或路由不可用时返回 uncertain 建议（附原因类型），
  绝不臆造"采纳/保留"；后端工厂返回 None（未配置模型）同样 uncertain，
  由端点如实映射 503；
- 只把两条冲突记忆的内容与记录时间交给模型，不携带会话原文。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, final
from uuid import uuid4

from app.harness.json_payload import load_json_object
from app.llm import CompletionRequest, CompletionResult, LLMMessage, LLMRoute
from app.schemas.common import PrivacyLevel

logger = logging.getLogger(__name__)

RECOMMENDATIONS = ("adopt", "keep", "uncertain")
_MAX_REASON = 200
_MAX_CONTENT = 400

_INSTRUCTION = (
    "你是记忆冲突裁决顾问。以下两条长期记忆互相冲突，请判断哪一条更可能是"
    "用户想保留的当前事实。\n"
    "新记忆（待裁决，conflict 状态）：{new_content}（记录于 {new_time}）\n"
    "旧记忆（当前生效）：{original_content}（记录于 {original_time}）\n"
    '只输出严格 JSON：{{"recommendation":"adopt|keep|uncertain","reason":"不超过80字"}}\n'
    "adopt=建议采纳新记忆；keep=建议保留旧记忆；依据不足时用 uncertain。"
    "reason 只依据两条记忆的内容与记录时间，不臆测。本建议仅供人参考，"
    "最终裁决由管理员做出。"
)


@dataclass(frozen=True, slots=True)
class ConflictAdvice:
    recommendation: str  # adopt | keep | uncertain
    reason: str


def _uncertain(reason: str) -> ConflictAdvice:
    return ConflictAdvice(recommendation="uncertain", reason=reason)


def _parse(text: str) -> ConflictAdvice:
    payload = load_json_object(text)
    recommendation = str(payload.get("recommendation", "uncertain"))
    if recommendation not in RECOMMENDATIONS:
        return _uncertain("unparseable_recommendation")
    return ConflictAdvice(
        recommendation=recommendation, reason=str(payload.get("reason", ""))[:_MAX_REASON]
    )


class ConflictAdvisor(Protocol):
    """冲突裁决建议端口；实现只读、不执行裁决。"""

    async def advise(
        self,
        *,
        new_content: str,
        original_content: str,
        new_created_at: str,
        original_created_at: str,
    ) -> ConflictAdvice: ...


class CompletionBackend(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResult: ...


@final
class LlmConflictAdvisor:
    """按需构建完成后台的裁决顾问；工厂返回 None 表示模型未配置。"""

    def __init__(self, backend_factory: Callable[[], CompletionBackend | None]) -> None:
        self._backend_factory = backend_factory

    async def advise(
        self,
        *,
        new_content: str,
        original_content: str,
        new_created_at: str,
        original_created_at: str,
    ) -> ConflictAdvice:
        backend = self._backend_factory()
        if backend is None:
            return _uncertain("model_unconfigured")
        request = CompletionRequest(
            trace_id=uuid4(),
            messages=[
                LLMMessage(
                    role="system",
                    content=_INSTRUCTION.format(
                        new_content=new_content[:_MAX_CONTENT],
                        original_content=original_content[:_MAX_CONTENT],
                        new_time=new_created_at,
                        original_time=original_created_at,
                    ),
                ),
                LLMMessage(role="user", content="请给出裁决建议。"),
            ],
            privacy_level=PrivacyLevel.L1,
            route=LLMRoute.UTILITY,
            temperature=0.1,
            json_mode=True,
            max_tokens=512,
        )
        try:
            result = await backend.complete(request)
            return _parse(result.text)
        except Exception as exc:
            logger.warning(
                "memory.conflict_advisor reason=advisor_failed error_type=%s", type(exc).__name__
            )
            return _uncertain(f"advisor_error:{type(exc).__name__}")
