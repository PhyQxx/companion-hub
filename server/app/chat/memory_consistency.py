"""生成后长期记忆一致性检查。

第一阶段只处理本轮通过 ``exact_fact`` 精确召回的助手自身稳定事实。
这类事实通常是名字、生日、身高、体重、三围等高确定性槽位。若模型
明确给出与 active Memory 不同的值，服务端在提交前要求同一路由重生成
一次；repair 仍冲突或调用失败时，直接用已知记忆生成保守降级回复。

该 Guard 不承担通用语义 NLI。没有显式事实陈述时保持原回复，避免把
自然语言差异误判成冲突。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.llm import CompletionRequest, CompletionResult, LLMMessage, ModelUsage
from app.memory import MemoryEntry, RetrievalResult, extract_assistant_fact_assertions
from app.persona import PersonaConfig

from .reply import parse_agent_reply


@dataclass(frozen=True, slots=True)
class MemoryConsistencyOutcome:
    result: CompletionResult
    checked: bool
    repaired: bool
    fallback_used: bool
    conflict_memory_ids: tuple[int, ...]


class MemoryConsistencyGuard:
    """对精确召回的助手自我事实执行确定性一致性保护。"""

    @staticmethod
    def requires_buffering(retrieval: RetrievalResult | None) -> bool:
        """exact fact 回合必须先完整校验，避免错误 delta 已经发给前端。"""

        return bool(_protected_memories(retrieval))

    async def enforce(
        self,
        *,
        result: CompletionResult,
        request: CompletionRequest,
        persona: PersonaConfig,
        retrieval: RetrievalResult | None,
        backend: object | None,
    ) -> MemoryConsistencyOutcome:
        protected = _protected_memories(retrieval)
        if not protected:
            return MemoryConsistencyOutcome(
                result=result,
                checked=False,
                repaired=False,
                fallback_used=False,
                conflict_memory_ids=(),
            )

        reply = parse_agent_reply(result.text, persona)
        conflicts = _conflicts(reply.text, protected)
        if not conflicts:
            return MemoryConsistencyOutcome(
                result=result,
                checked=True,
                repaired=False,
                fallback_used=False,
                conflict_memory_ids=(),
            )

        repair_result: CompletionResult | None = None
        if backend is not None and hasattr(backend, "complete"):
            repair_request = _repair_request(request, conflicts)
            try:
                repair_result = await backend.complete(repair_request)
            except Exception:
                repair_result = None

        if repair_result is not None:
            repaired_reply = parse_agent_reply(repair_result.text, persona)
            remaining = _conflicts(repaired_reply.text, protected)
            if not remaining:
                return MemoryConsistencyOutcome(
                    result=_merge_usage(result, repair_result),
                    checked=True,
                    repaired=True,
                    fallback_used=False,
                    conflict_memory_ids=tuple(memory.id for memory in conflicts),
                )

        fallback = _fallback_text(conflicts)
        fallback_result = result.model_copy(update={"text": fallback})
        if repair_result is not None:
            fallback_result = _merge_usage(result, repair_result).model_copy(
                update={"text": fallback}
            )
        return MemoryConsistencyOutcome(
            result=fallback_result,
            checked=True,
            repaired=False,
            fallback_used=True,
            conflict_memory_ids=tuple(memory.id for memory in conflicts),
        )


def _protected_memories(retrieval: RetrievalResult | None) -> tuple[MemoryEntry, ...]:
    if retrieval is None:
        return ()
    by_slot: dict[str, MemoryEntry] = {}
    for hit in retrieval.hits:
        memory = hit.memory
        if (
            "exact_fact" not in hit.reasons
            or memory.subject_kind != "assistant"
            or not memory.fact_key
        ):
            continue
        # 排序已经把 subject_hint / importance 纳入 score。同一槽位如果有多个
        # exact 候选，只取当前结果里排名最前的 active 事实作为保护值。
        by_slot.setdefault(memory.fact_key, memory)
    return tuple(by_slot.values())


def _conflicts(text: str, protected: tuple[MemoryEntry, ...]) -> tuple[MemoryEntry, ...]:
    asserted = dict(extract_assistant_fact_assertions(text))
    conflicts: list[MemoryEntry] = []
    for memory in protected:
        fact_key = memory.fact_key
        if fact_key is None or fact_key not in asserted:
            continue
        if _normalize(asserted[fact_key]) != _normalize(memory.content):
            conflicts.append(memory)
    return tuple(conflicts)


def _repair_request(
    request: CompletionRequest, conflicts: tuple[MemoryEntry, ...]
) -> CompletionRequest:
    facts = "\n".join(
        f"- {memory.fact_key}: {memory.content}" for memory in conflicts if memory.fact_key
    )
    instruction = (
        "\n\n【一致性修复】你上一版候选回复与本轮已确认的 active 长期记忆冲突。"
        "请重新回答用户原问题，只修正冲突事实，不解释内部记忆系统，也不要改变无关内容。"
        "以下事实必须保持一致：\n"
        + facts
    )
    messages = list(request.messages)
    first = messages[0]
    if first.role == "system":
        messages[0] = LLMMessage(role="system", content=first.content + instruction)
    else:
        messages.insert(0, LLMMessage(role="system", content=instruction.strip()))
    return request.model_copy(
        update={
            "messages": messages,
            "temperature": min(float(request.temperature), 0.1),
        }
    )


def _fallback_text(conflicts: tuple[MemoryEntry, ...]) -> str:
    if len(conflicts) == 1:
        content = _first_person(conflicts[0].content)
        return f"按我之前明确说过的，{content}。"
    facts = "；".join(_first_person(memory.content) for memory in conflicts)
    return f"按我之前明确说过的信息，{facts}。"


def _first_person(content: str) -> str:
    if content.startswith("助手"):
        return "我" + content[len("助手") :]
    return content


def _normalize(content: str) -> str:
    return "".join(content.split()).rstrip("。.!")


def _merge_usage(original: CompletionResult, repair: CompletionResult) -> CompletionResult:
    usage = ModelUsage(
        input_tokens=original.usage.input_tokens + repair.usage.input_tokens,
        output_tokens=original.usage.output_tokens + repair.usage.output_tokens,
        total_tokens=original.usage.total_tokens + repair.usage.total_tokens,
        estimated_cost=original.usage.estimated_cost + repair.usage.estimated_cost,
    )
    return repair.model_copy(
        update={
            "usage": usage,
            "latency_ms": original.latency_ms + repair.latency_ms,
        }
    )
