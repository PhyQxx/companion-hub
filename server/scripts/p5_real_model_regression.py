# ruff: noqa: RUF001
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from app.chat import ChatService  # noqa: E402
from app.config import ConfigStore  # noqa: E402
from app.db import (  # noqa: E402
    AppUserRecord,
    Base,
    ConversationRecord,
    Database,
    MessageRecord,
    create_database,
)
from app.ids import uuid7  # noqa: E402
from app.llm import LLMRouteExhausted  # noqa: E402
from app.memory import (  # noqa: E402
    MemoryCandidate,
    MemoryOriginKind,
    MemorySourceKind,
    MemorySourceRef,
    MemoryStatus,
    MemoryStore,
    MemorySubjectKind,
    MemoryType,
)
from app.schemas import PrivacyLevel  # noqa: E402
from app.timeline import HistoryRecallService, TimelineActor, TimelineStore  # noqa: E402


@dataclass(frozen=True, slots=True)
class QueryCase:
    case_id: str
    category: str
    prompt: str
    expected_all: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    subject: str | None = None
    fact_key: str | None = None
    privacy_level: PrivacyLevel = PrivacyLevel.L1
    expected_recall_mode: str | None = None


@dataclass(slots=True)
class CaseResult:
    case_id: str
    category: str
    status: str
    answer: str = ""
    reason: str = ""
    elapsed_ms: float = 0.0
    recall_mode: str | None = None
    memory_hits: list[dict[str, Any]] | None = None
    model_attempts: int = 1


@dataclass(slots=True)
class Runtime:
    database: Database
    config_store: ConfigStore
    memory: MemoryStore
    timeline: TimelineStore
    chat: ChatService
    user_id: UUID
    timezone_name: str


QUERY_CASES: tuple[QueryCase, ...] = (
    QueryCase(
        "assistant-height",
        "assistant_fact",
        "你的身高是多少？",
        expected_all=("160",),
        subject="assistant",
        fact_key="profile.height",
        expected_recall_mode="memory",
    ),
    QueryCase(
        "assistant-height-paraphrase",
        "assistant_fact",
        "你多高来着？",
        expected_all=("160",),
        subject="assistant",
        fact_key="profile.height",
        expected_recall_mode="memory",
    ),
    QueryCase(
        "assistant-weight",
        "assistant_fact",
        "你的体重是多少？",
        expected_all=("48",),
        subject="assistant",
        fact_key="profile.weight",
        expected_recall_mode="memory",
    ),
    QueryCase(
        "assistant-birthday",
        "assistant_fact",
        "你的生日是哪天？",
        expected_all=("12", "27"),
        subject="assistant",
        fact_key="profile.birthday",
        expected_recall_mode="memory",
    ),
    QueryCase(
        "assistant-measurements",
        "assistant_fact",
        "你的三围是多少？",
        expected_all=("91", "63", "88"),
        subject="assistant",
        fact_key="profile.measurements",
        expected_recall_mode="memory",
    ),
    QueryCase(
        "assistant-nickname",
        "assistant_fact",
        "你的昵称是什么？",
        expected_all=("小葵",),
        subject="assistant",
        fact_key="profile.nickname",
        expected_recall_mode="memory",
    ),
    QueryCase(
        "assistant-food",
        "assistant_preference",
        "你喜欢吃什么？",
        expected_all=("桂花糕",),
        subject="assistant",
        fact_key="preference.food",
        expected_recall_mode="memory",
    ),
    QueryCase(
        "assistant-drink",
        "assistant_preference",
        "你喜欢喝什么？",
        expected_all=("茉莉花茶",),
        subject="assistant",
        fact_key="preference.drink",
        expected_recall_mode="memory",
    ),
    QueryCase(
        "user-name",
        "user_fact",
        "我叫什么名字？",
        expected_all=("浩宇",),
        subject="user",
        fact_key="profile.name",
        expected_recall_mode="memory",
    ),
    QueryCase(
        "user-height-isolation",
        "subject_isolation",
        "我的身高是多少？",
        expected_all=("180",),
        subject="user",
        fact_key="profile.height",
        expected_recall_mode="memory",
    ),
    QueryCase(
        "user-food",
        "user_preference",
        "我是不是不吃香菜？",
        expected_all=("香菜", "不"),
        subject="user",
        fact_key="preference.food",
        expected_recall_mode="memory",
    ),
    QueryCase(
        "user-drink",
        "user_preference",
        "我喜欢喝什么？",
        expected_all=("黑咖啡",),
        subject="user",
        fact_key="preference.drink",
        expected_recall_mode="memory",
    ),
    QueryCase(
        "shared-movie",
        "shared_commitment",
        "我们周末约好一起看什么？",
        expected_all=("星际穿越",),
        subject="shared",
        expected_recall_mode="memory",
    ),
    QueryCase(
        "timeline-recent",
        "timeline",
        "刚才我说午饭吃了什么？",
        expected_all=("番茄鸡蛋面",),
        expected_recall_mode="source",
    ),
    QueryCase(
        "timeline-yesterday-evening",
        "timeline",
        "我昨天晚上说想看的电影是什么？",
        expected_all=("银翼杀手2049",),
        expected_recall_mode="source",
    ),
    QueryCase(
        "timeline-no-evidence",
        "timeline",
        "昨天我说过量子龙虾吗？",
        expected_recall_mode="none",
    ),
)

DYNAMIC_CASE_IDS = (
    "assistant-profile-bootstrap",
    "conflict-active-wins",
    "deletion-does-not-resurrect",
    "l2-isolation",
    "conversation-extraction",
)


def _source(case_id: str) -> list[MemorySourceRef]:
    return [
        MemorySourceRef(
            source_kind=MemorySourceKind.MANUAL,
            source_id=f"p5-real-model:{case_id}",
        )
    ]


async def _seed_memory(
    runtime: Runtime,
    *,
    case_id: str,
    subject: MemorySubjectKind,
    subject_key: str,
    memory_type: MemoryType,
    content: str,
    fact_key: str | None = None,
    privacy_level: PrivacyLevel = PrivacyLevel.L1,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    conflict_with: int | None = None,
) -> int:
    created = await runtime.memory.add(
        MemoryCandidate(
            subject_kind=subject,
            subject_key=subject_key,
            fact_key=fact_key,
            origin_kind=MemoryOriginKind.MANUAL,
            type=memory_type,
            content=content,
            privacy_level=privacy_level,
            sources=_source(case_id),
            importance=0.85,
            confidence=1.0,
            extractor_version="p5-real-model-seed-v1",
        ),
        user_id=runtime.user_id,
        actor="p5-regression",
        status=status,
        conflict_with=conflict_with,
    )
    return created.id


async def _seed_baseline(runtime: Runtime) -> dict[str, int]:
    ids: dict[str, int] = {}
    ids["assistant-height"] = await _seed_memory(
        runtime,
        case_id="assistant-height",
        subject=MemorySubjectKind.ASSISTANT,
        subject_key="assistant:primary",
        memory_type=MemoryType.SEMANTIC,
        content="助手身高为 160 厘米",
        fact_key="profile.height",
    )
    await _seed_memory(
        runtime,
        case_id="assistant-weight",
        subject=MemorySubjectKind.ASSISTANT,
        subject_key="assistant:primary",
        memory_type=MemoryType.SEMANTIC,
        content="助手体重为 48 公斤",
        fact_key="profile.weight",
    )
    await _seed_memory(
        runtime,
        case_id="assistant-birthday",
        subject=MemorySubjectKind.ASSISTANT,
        subject_key="assistant:primary",
        memory_type=MemoryType.SEMANTIC,
        content="助手生日为 12月27日",
        fact_key="profile.birthday",
    )
    await _seed_memory(
        runtime,
        case_id="assistant-measurements",
        subject=MemorySubjectKind.ASSISTANT,
        subject_key="assistant:primary",
        memory_type=MemoryType.SEMANTIC,
        content="助手三围为 91-63-88 厘米",
        fact_key="profile.measurements",
    )
    await _seed_memory(
        runtime,
        case_id="assistant-nickname",
        subject=MemorySubjectKind.ASSISTANT,
        subject_key="assistant:primary",
        memory_type=MemoryType.SEMANTIC,
        content="助手昵称为小葵",
        fact_key="profile.nickname",
    )
    await _seed_memory(
        runtime,
        case_id="assistant-food",
        subject=MemorySubjectKind.ASSISTANT,
        subject_key="assistant:primary",
        memory_type=MemoryType.PREFERENCE,
        content="助手喜欢桂花糕",
        fact_key="preference.food",
    )
    await _seed_memory(
        runtime,
        case_id="assistant-drink",
        subject=MemorySubjectKind.ASSISTANT,
        subject_key="assistant:primary",
        memory_type=MemoryType.PREFERENCE,
        content="助手喜欢茉莉花茶",
        fact_key="preference.drink",
    )
    await _seed_memory(
        runtime,
        case_id="user-name",
        subject=MemorySubjectKind.USER,
        subject_key="user:self",
        memory_type=MemoryType.SEMANTIC,
        content="用户名字为浩宇",
        fact_key="profile.name",
    )
    await _seed_memory(
        runtime,
        case_id="user-height",
        subject=MemorySubjectKind.USER,
        subject_key="user:self",
        memory_type=MemoryType.SEMANTIC,
        content="用户身高为 180 厘米",
        fact_key="profile.height",
    )
    await _seed_memory(
        runtime,
        case_id="user-food",
        subject=MemorySubjectKind.USER,
        subject_key="user:self",
        memory_type=MemoryType.PREFERENCE,
        content="用户不吃香菜",
        fact_key="preference.food",
    )
    await _seed_memory(
        runtime,
        case_id="user-drink",
        subject=MemorySubjectKind.USER,
        subject_key="user:self",
        memory_type=MemoryType.PREFERENCE,
        content="用户喜欢黑咖啡",
        fact_key="preference.drink",
    )
    await _seed_memory(
        runtime,
        case_id="shared-movie",
        subject=MemorySubjectKind.SHARED,
        subject_key="shared:user-assistant",
        memory_type=MemoryType.COMMITMENT,
        content="双方约定：周末一起看《星际穿越》",
    )
    return ids


async def _seed_timeline_message(
    runtime: Runtime, *, text: str, occurred_at: datetime, title: str
) -> None:
    conversation_id = uuid7()
    message_id = uuid7()
    turn_id = uuid7()
    moment = occurred_at.astimezone(UTC)
    async with runtime.database.sessions.begin() as session:
        session.add(
            ConversationRecord(
                id=conversation_id,
                user_id=runtime.user_id,
                title=title,
                status="active",
                last_seq=1,
                last_turn_seq=1,
                created_at=moment,
                last_active_at=moment,
            )
        )
        session.add(
            MessageRecord(
                id=message_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                seq=1,
                role="user",
                content=text,
                privacy_level=PrivacyLevel.L1.value,
                created_at=moment,
            )
        )
    await runtime.timeline.index_message(
        user_id=runtime.user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        actor=TimelineActor.USER,
        text=text,
        privacy_level=PrivacyLevel.L1,
        occurred_at=occurred_at,
    )


async def _seed_timeline(runtime: Runtime) -> None:
    zone = ZoneInfo(runtime.timezone_name)
    now = datetime.now(zone)
    await _seed_timeline_message(
        runtime,
        text="我今天午饭吃了番茄鸡蛋面。",
        occurred_at=now - timedelta(minutes=8),
        title="p5 recent timeline",
    )
    yesterday_evening = (now - timedelta(days=1)).replace(
        hour=21, minute=30, second=0, microsecond=0
    )
    await _seed_timeline_message(
        runtime,
        text="我想看《银翼杀手2049》。",
        occurred_at=yesterday_evening,
        title="p5 yesterday timeline",
    )


def _memory_hits(meta: dict[str, object] | None) -> list[dict[str, Any]]:
    if not meta:
        return []
    memory = meta.get("memory")
    if not isinstance(memory, dict):
        return []
    hits = memory.get("hits")
    if not isinstance(hits, list):
        return []
    return [item for item in hits if isinstance(item, dict)]


def _recall_mode(meta: dict[str, object] | None) -> str | None:
    if not meta:
        return None
    recall = meta.get("recall")
    if not isinstance(recall, dict):
        return None
    mode = recall.get("mode")
    return str(mode) if mode is not None else None


async def _ask(runtime: Runtime, case_id: str, prompt: str, privacy: PrivacyLevel) -> Any:
    conversation = await runtime.chat.create_conversation(
        user_id=runtime.user_id, title=f"P5 {case_id}"
    )
    return await runtime.chat.send_message(
        conversation.id,
        user_id=runtime.user_id,
        text=prompt,
        privacy_level=privacy,
    )


def _route_diagnostics(error: LLMRouteExhausted) -> str:
    return ", ".join(
        f"{item.endpoint}#{item.attempt}:{item.error_type}"
        for item in error.failures
    )


def _route_retry_delay(error: LLMRouteExhausted) -> float:
    if any(item.error_type == "RateLimitError" for item in error.failures):
        return 30.0
    return 1.5


async def _with_route_retries(
    operation: Callable[[], Awaitable[Any]],
    *,
    route_retries: int,
) -> tuple[Any, int]:
    attempts = 0
    while True:
        attempts += 1
        try:
            return await operation(), attempts
        except LLMRouteExhausted as error:
            if attempts > route_retries:
                raise
            await asyncio.sleep(_route_retry_delay(error))


async def _run_query_case(
    runtime: Runtime,
    case: QueryCase,
    *,
    route_retries: int = 1,
) -> CaseResult:
    started = time.perf_counter()
    try:
        turn, model_attempts = await _with_route_retries(
            lambda: _ask(runtime, case.case_id, case.prompt, case.privacy_level),
            route_retries=route_retries,
        )
    except LLMRouteExhausted as error:
        diagnostics = _route_diagnostics(error)
        suffix = f" [{diagnostics}]" if diagnostics else ""
        return CaseResult(
            case_id=case.case_id,
            category=case.category,
            status="failed",
            reason=f"model route failed: {error.reason_code}{suffix}",
            elapsed_ms=(time.perf_counter() - started) * 1000,
            model_attempts=route_retries + 1,
        )
    except Exception as error:
        return CaseResult(
            case_id=case.case_id,
            category=case.category,
            status="failed",
            reason=f"model call failed: {type(error).__name__}: {error}",
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
    answer = turn.assistant_message.content
    meta = turn.assistant_message.decision_meta
    hits = _memory_hits(meta)
    recall = _recall_mode(meta)
    failures: list[str] = []
    for expected in case.expected_all:
        if expected not in answer:
            failures.append(f"missing answer token {expected!r}")
    for forbidden in case.forbidden:
        if forbidden in answer:
            failures.append(f"forbidden answer token {forbidden!r}")
    if case.expected_recall_mode and recall != case.expected_recall_mode:
        failures.append(
            f"recall mode {recall!r} != {case.expected_recall_mode!r}"
        )
    if case.subject is not None:
        matching = [item for item in hits if item.get("subject") == case.subject]
        if not matching:
            failures.append(f"no memory hit for subject {case.subject!r}")
        elif case.fact_key is not None and not any(
            item.get("fact_key") == case.fact_key for item in matching
        ):
            failures.append(f"no memory hit for fact_key {case.fact_key!r}")
    return CaseResult(
        case_id=case.case_id,
        category=case.category,
        status="passed" if not failures else "failed",
        answer=answer,
        reason="; ".join(failures),
        elapsed_ms=(time.perf_counter() - started) * 1000,
        recall_mode=recall,
        memory_hits=hits,
        model_attempts=model_attempts,
    )


async def _run_assistant_profile_bootstrap_case(
    runtime: Runtime,
    *,
    route_retries: int = 1,
) -> CaseResult:
    """A fresh companion should establish a human-like self profile, then remember it."""

    started = time.perf_counter()
    fresh_user = AppUserRecord(
        id=uuid7(),
        display_name="P5 Embodied Companion",
        locale="zh-CN",
        timezone=runtime.timezone_name,
        status="active",
    )
    async with runtime.database.sessions.begin() as session:
        session.add(fresh_user)

    first_conversation = await runtime.chat.create_conversation(
        user_id=fresh_user.id, title="P5 embodied profile establish"
    )
    try:
        first_turn, first_attempts = await _with_route_retries(
            lambda: runtime.chat.send_message(
                first_conversation.id,
                user_id=fresh_user.id,
                text=(
                    "对了，我想记录下你的一些信息。告诉我你的身高、体重、三围，"
                    "请直接给出明确数值。"
                ),
                privacy_level=PrivacyLevel.L1,
            ),
            route_retries=route_retries,
        )
    except LLMRouteExhausted as error:
        diagnostics = _route_diagnostics(error)
        suffix = f" [{diagnostics}]" if diagnostics else ""
        return CaseResult(
            "assistant-profile-bootstrap",
            "assistant_identity",
            "failed",
            reason=f"first model route failed: {error.reason_code}{suffix}",
            elapsed_ms=(time.perf_counter() - started) * 1000,
            model_attempts=route_retries + 1,
        )

    first_answer = first_turn.assistant_message.content
    failures: list[str] = []
    refusal_fragments = (
        "没有真实的物理身体",
        "没有真实身体",
        "没有物理身体",
        "所以没有身高",
        "所以没有体重",
        "所以没有三围",
        "只是一个虚拟的AI",
        "只是一个虚拟的 ai",
    )
    if any(fragment.lower() in first_answer.lower() for fragment in refusal_fragments):
        failures.append("assistant rejected role self-profile as if it required a physical body")

    expected_fact_keys = (
        "profile.height",
        "profile.weight",
        "profile.measurements",
    )
    stored: dict[str, str] = {}
    for fact_key in expected_fact_keys:
        rows = await runtime.memory.list_memories(
            user_id=fresh_user.id,
            subject_kind=MemorySubjectKind.ASSISTANT,
            subject_key="assistant:primary",
            fact_key=fact_key,
            status=MemoryStatus.ACTIVE,
            limit=10,
        )
        if not rows:
            failures.append(f"missing persisted assistant fact: {fact_key}")
            continue
        stored[fact_key] = rows[0].content

    second_attempts = 0
    second_answer = ""
    second_meta: dict[str, object] | None = None
    if len(stored) == len(expected_fact_keys):
        second_conversation = await runtime.chat.create_conversation(
            user_id=fresh_user.id, title="P5 embodied profile recall"
        )
        try:
            second_turn, second_attempts = await _with_route_retries(
                lambda: runtime.chat.send_message(
                    second_conversation.id,
                    user_id=fresh_user.id,
                    text="再告诉我一次你的身高、体重和三围。",
                    privacy_level=PrivacyLevel.L1,
                ),
                route_retries=route_retries,
            )
            second_answer = second_turn.assistant_message.content
            second_meta = second_turn.assistant_message.decision_meta
        except LLMRouteExhausted as error:
            diagnostics = _route_diagnostics(error)
            suffix = f" [{diagnostics}]" if diagnostics else ""
            failures.append(f"second model route failed: {error.reason_code}{suffix}")

    if second_answer:
        for fact_key, content in stored.items():
            values = re.findall(r"\d+(?:\.\d+)?", content)
            if not values or any(value not in second_answer for value in values):
                failures.append(f"second session drifted from persisted {fact_key}: {content}")
        hits = _memory_hits(second_meta)
        exact_grounded = {
            item.get("fact_key")
            for item in hits
            if item.get("subject") == "assistant"
            and item.get("grounded") is True
            and "exact_fact" in (item.get("reasons") or [])
        }
        if not set(expected_fact_keys).issubset(exact_grounded):
            failures.append("second session did not exact-recall all three assistant profile slots")

    return CaseResult(
        "assistant-profile-bootstrap",
        "assistant_identity",
        "passed" if not failures else "failed",
        answer=(
            f"first={first_answer}\nsecond={second_answer}" if second_answer else first_answer
        ),
        reason="; ".join(failures),
        elapsed_ms=(time.perf_counter() - started) * 1000,
        recall_mode=_recall_mode(second_meta),
        memory_hits=_memory_hits(second_meta),
        model_attempts=first_attempts + second_attempts,
    )


async def _run_conflict_case(
    runtime: Runtime,
    active_height_id: int,
    *,
    route_retries: int = 1,
) -> CaseResult:
    await _seed_memory(
        runtime,
        case_id="assistant-height-conflict",
        subject=MemorySubjectKind.ASSISTANT,
        subject_key="assistant:primary",
        memory_type=MemoryType.SEMANTIC,
        content="助手身高为 165 厘米",
        fact_key="profile.height",
        status=MemoryStatus.CONFLICT,
        conflict_with=active_height_id,
    )
    result = await _run_query_case(
        runtime,
        QueryCase(
            "conflict-active-wins",
            "conflict",
            "你的身高是多少？",
            expected_all=("160",),
            forbidden=("165",),
            subject="assistant",
            fact_key="profile.height",
            expected_recall_mode="memory",
        ),
        route_retries=route_retries,
    )
    conflicts = await runtime.memory.list_memories(
        user_id=runtime.user_id,
        subject_kind=MemorySubjectKind.ASSISTANT,
        subject_key="assistant:primary",
        fact_key="profile.height",
        status=MemoryStatus.CONFLICT,
        limit=10,
    )
    if not conflicts:
        result.status = "failed"
        result.reason = (result.reason + "; " if result.reason else "") + "conflict row missing"
    return result


async def _run_deletion_case(
    runtime: Runtime,
    *,
    route_retries: int = 1,
) -> CaseResult:
    started = time.perf_counter()
    conversation = await runtime.chat.create_conversation(
        user_id=runtime.user_id, title="P5 deletion source"
    )
    try:
        _, setup_attempts = await _with_route_retries(
            lambda: runtime.chat.send_message(
                conversation.id,
                user_id=runtime.user_id,
                text="我喜欢榛果拿铁。",
                privacy_level=PrivacyLevel.L1,
            ),
            route_retries=route_retries,
        )
    except LLMRouteExhausted as error:
        diagnostics = _route_diagnostics(error)
        suffix = f" [{diagnostics}]" if diagnostics else ""
        return CaseResult(
            "deletion-does-not-resurrect",
            "deletion",
            "failed",
            reason=f"setup model route failed: {error.reason_code}{suffix}",
            elapsed_ms=(time.perf_counter() - started) * 1000,
            model_attempts=route_retries + 1,
        )
    except Exception as error:
        return CaseResult(
            "deletion-does-not-resurrect",
            "deletion",
            "failed",
            reason=f"setup model call failed: {type(error).__name__}: {error}",
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
    before = await runtime.memory.list_memories(user_id=runtime.user_id, limit=200)
    setup_found = any("榛果拿铁" in item.content for item in before)
    await runtime.chat.delete_conversation(conversation.id, user_id=runtime.user_id)
    after = await runtime.memory.list_memories(user_id=runtime.user_id, limit=200)
    deleted = not any("榛果拿铁" in item.content for item in after)
    try:
        turn, verify_attempts = await _with_route_retries(
            lambda: _ask(
                runtime,
                "deletion-does-not-resurrect",
                "之前我喜欢什么拿铁？",
                PrivacyLevel.L1,
            ),
            route_retries=route_retries,
        )
    except LLMRouteExhausted as error:
        diagnostics = _route_diagnostics(error)
        suffix = f" [{diagnostics}]" if diagnostics else ""
        return CaseResult(
            "deletion-does-not-resurrect",
            "deletion",
            "failed",
            reason=f"verification model route failed: {error.reason_code}{suffix}",
            elapsed_ms=(time.perf_counter() - started) * 1000,
            model_attempts=setup_attempts + route_retries + 1,
        )
    except Exception as error:
        return CaseResult(
            "deletion-does-not-resurrect",
            "deletion",
            "failed",
            reason=f"verification model call failed: {type(error).__name__}: {error}",
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
    answer = turn.assistant_message.content
    failures: list[str] = []
    if not setup_found:
        failures.append("setup memory was not extracted")
    if not deleted:
        failures.append("source-derived memory survived conversation deletion")
    if "榛果拿铁" in answer:
        failures.append("deleted fact resurfaced in answer")
    return CaseResult(
        "deletion-does-not-resurrect",
        "deletion",
        "passed" if not failures else "failed",
        answer=answer,
        reason="; ".join(failures),
        elapsed_ms=(time.perf_counter() - started) * 1000,
        recall_mode=_recall_mode(turn.assistant_message.decision_meta),
        memory_hits=_memory_hits(turn.assistant_message.decision_meta),
        model_attempts=setup_attempts + verify_attempts,
    )


async def _run_l2_case(
    runtime: Runtime,
    *,
    route_retries: int = 1,
) -> CaseResult:
    started = time.perf_counter()
    await _seed_memory(
        runtime,
        case_id="l2-secret-code",
        subject=MemorySubjectKind.ASSISTANT,
        subject_key="assistant:primary",
        memory_type=MemoryType.SEMANTIC,
        content="助手秘密代号为月见",
        fact_key="profile.secret_code",
        privacy_level=PrivacyLevel.L2,
    )
    try:
        public, public_attempts = await _with_route_retries(
            lambda: _ask(
                runtime,
                "l2-isolation-public",
                "之前你的秘密代号是什么？",
                PrivacyLevel.L1,
            ),
            route_retries=route_retries,
        )
        private, private_attempts = await _with_route_retries(
            lambda: _ask(
                runtime,
                "l2-isolation-private",
                "之前你的秘密代号是什么？",
                PrivacyLevel.L2,
            ),
            route_retries=route_retries,
        )
    except LLMRouteExhausted as error:
        diagnostics = _route_diagnostics(error)
        suffix = f" [{diagnostics}]" if diagnostics else ""
        return CaseResult(
            "l2-isolation",
            "privacy",
            "failed",
            reason=f"L2 route/model failed: {error.reason_code}{suffix}",
            elapsed_ms=(time.perf_counter() - started) * 1000,
            model_attempts=route_retries + 1,
        )
    except Exception as error:
        return CaseResult(
            "l2-isolation",
            "privacy",
            "failed",
            reason=f"L2 route/model call failed: {type(error).__name__}: {error}",
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
    public_answer = public.assistant_message.content
    private_answer = private.assistant_message.content
    public_hits = _memory_hits(public.assistant_message.decision_meta)
    private_hits = _memory_hits(private.assistant_message.decision_meta)
    failures: list[str] = []
    if "月见" in public_answer:
        failures.append("L2 fact leaked into L1 answer")
    if any(item.get("fact_key") == "profile.secret_code" for item in public_hits):
        failures.append("L2 fact leaked into L1 memory hits")
    if "月见" not in private_answer:
        failures.append("L2 answer did not recall the secret code")
    if not any(item.get("fact_key") == "profile.secret_code" for item in private_hits):
        failures.append("L2 memory hit missing in private turn")
    return CaseResult(
        "l2-isolation",
        "privacy",
        "passed" if not failures else "failed",
        answer=f"L1={public_answer}\nL2={private_answer}",
        reason="; ".join(failures),
        elapsed_ms=(time.perf_counter() - started) * 1000,
        recall_mode=_recall_mode(private.assistant_message.decision_meta),
        memory_hits=private_hits,
        model_attempts=public_attempts + private_attempts,
    )


async def _run_extraction_case(
    runtime: Runtime,
    *,
    route_retries: int = 1,
) -> CaseResult:
    started = time.perf_counter()
    try:
        conversation = await runtime.chat.create_conversation(
            user_id=runtime.user_id, title="P5 extraction"
        )
        _, model_attempts = await _with_route_retries(
            lambda: runtime.chat.send_message(
                conversation.id,
                user_id=runtime.user_id,
                text="记住，你的身高是162厘米。只需简短确认。",
                privacy_level=PrivacyLevel.L1,
            ),
            route_retries=route_retries,
        )
    except LLMRouteExhausted as error:
        diagnostics = _route_diagnostics(error)
        suffix = f" [{diagnostics}]" if diagnostics else ""
        return CaseResult(
            "conversation-extraction",
            "extraction",
            "failed",
            reason=f"setup model route failed: {error.reason_code}{suffix}",
            elapsed_ms=(time.perf_counter() - started) * 1000,
            model_attempts=route_retries + 1,
        )
    except Exception as error:
        return CaseResult(
            "conversation-extraction",
            "extraction",
            "failed",
            reason=f"setup model call failed: {type(error).__name__}: {error}",
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
    conflicts = await runtime.memory.list_memories(
        user_id=runtime.user_id,
        subject_kind=MemorySubjectKind.ASSISTANT,
        subject_key="assistant:primary",
        fact_key="profile.height",
        status=MemoryStatus.CONFLICT,
        limit=20,
    )
    found = any("162" in item.content for item in conflicts)
    return CaseResult(
        "conversation-extraction",
        "extraction",
        "passed" if found else "failed",
        reason="" if found else "completed turn did not create the expected assistant conflict",
        elapsed_ms=(time.perf_counter() - started) * 1000,
        model_attempts=model_attempts,
    )


async def _build_runtime(config_path: Path, timezone_name: str) -> Runtime:
    database = create_database("sqlite+aiosqlite:///:memory:")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    config_store = ConfigStore(config_path)
    await config_store.load()
    user = AppUserRecord(
        id=uuid7(),
        display_name="P5 Real Model Regression",
        locale="zh-CN",
        timezone=timezone_name,
        status="active",
    )
    async with database.sessions.begin() as session:
        session.add(user)
    memory = MemoryStore(database)
    timeline = TimelineStore(database)
    chat = ChatService(
        database,
        config_store,
        memory_store=memory,
        timeline_store=timeline,
        history_recall_service=HistoryRecallService(
            timeline, timezone_name=timezone_name
        ),
    )
    return Runtime(
        database=database,
        config_store=config_store,
        memory=memory,
        timeline=timeline,
        chat=chat,
        user_id=user.id,
        timezone_name=timezone_name,
    )


def _all_case_ids() -> tuple[str, ...]:
    return tuple(case.case_id for case in QUERY_CASES) + DYNAMIC_CASE_IDS


def _load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE lines without logging values.

    Existing process variables win over the file so CI/production-injected secrets are never
    silently replaced by a developer .env file.
    """

    if not path.is_file():
        raise FileNotFoundError(f"env file not found: {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


async def _run(args: argparse.Namespace) -> int:
    if args.env_file:
        try:
            _load_env_file(Path(args.env_file).expanduser().resolve())
        except OSError as error:
            print(str(error), file=sys.stderr)
            return 2
    config_value = args.config or os.getenv("ARIA_CONFIG_PATH")
    if not config_value:
        print("missing model config: pass --config or set ARIA_CONFIG_PATH", file=sys.stderr)
        return 2
    config_path = Path(config_value).expanduser().resolve()
    if not config_path.is_file():
        print(f"config not found: {config_path}", file=sys.stderr)
        return 2
    all_case_ids = set(_all_case_ids())
    selected = set(args.case or all_case_ids)
    excluded = set(getattr(args, "exclude_case", None) or ())
    unknown = (selected | excluded) - all_case_ids
    if unknown:
        print(f"unknown case ids: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    selected -= excluded
    if not selected:
        print("no cases selected after exclusions", file=sys.stderr)
        return 2
    runtime = await _build_runtime(config_path, args.timezone)
    started_at = datetime.now(UTC)
    results: list[CaseResult] = []
    try:
        baseline = await _seed_baseline(runtime)
        await _seed_timeline(runtime)
        for case in QUERY_CASES:
            if case.case_id in selected:
                result = await _run_query_case(
                    runtime,
                    case,
                    route_retries=args.route_retries,
                )
                results.append(result)
                detail = result.reason or result.answer
                print(f"[{result.status.upper():6}] {result.case_id}: {detail}")
                if args.delay_ms:
                    await asyncio.sleep(args.delay_ms / 1_000)
        if "assistant-profile-bootstrap" in selected:
            result = await _run_assistant_profile_bootstrap_case(
                runtime,
                route_retries=args.route_retries,
            )
            results.append(result)
            print(f"[{result.status.upper():6}] {result.case_id}: {result.reason or result.answer}")
            if args.delay_ms:
                await asyncio.sleep(args.delay_ms / 1_000)
        if "conflict-active-wins" in selected:
            result = await _run_conflict_case(
                runtime,
                baseline["assistant-height"],
                route_retries=args.route_retries,
            )
            results.append(result)
            print(f"[{result.status.upper():6}] {result.case_id}: {result.reason or result.answer}")
            if args.delay_ms:
                await asyncio.sleep(args.delay_ms / 1_000)
        if "deletion-does-not-resurrect" in selected:
            result = await _run_deletion_case(
                runtime,
                route_retries=args.route_retries,
            )
            results.append(result)
            print(f"[{result.status.upper():6}] {result.case_id}: {result.reason or result.answer}")
            if args.delay_ms:
                await asyncio.sleep(args.delay_ms / 1_000)
        if "l2-isolation" in selected:
            result = await _run_l2_case(
                runtime,
                route_retries=args.route_retries,
            )
            results.append(result)
            print(f"[{result.status.upper():6}] {result.case_id}: {result.reason or result.answer}")
            if args.delay_ms:
                await asyncio.sleep(args.delay_ms / 1_000)
        if "conversation-extraction" in selected:
            result = await _run_extraction_case(
                runtime,
                route_retries=args.route_retries,
            )
            results.append(result)
            print(f"[{result.status.upper():6}] {result.case_id}: {result.reason or 'ok'}")
    finally:
        await runtime.database.close()

    passed = sum(item.status == "passed" for item in results)
    failed = sum(item.status == "failed" for item in results)
    report = {
        "schema_version": 1,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "config_path": str(config_path),
        "timezone": args.timezone,
        "selected_cases": sorted(selected),
        "summary": {"passed": passed, "failed": failed, "total": len(results)},
        "cases": [asdict(item) for item in results],
    }
    if args.report:
        report_path = Path(args.report).expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"report: {report_path}")
    print(f"summary: {passed} passed, {failed} failed, {len(results)} total")
    return 0 if failed == 0 and len(results) == len(selected) else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run isolated P5 real-model Memory/Timeline regression cases."
    )
    parser.add_argument(
        "--config",
        help="Hub YAML used only for model routing. Falls back to ARIA_CONFIG_PATH.",
    )
    parser.add_argument(
        "--env-file",
        help="Optional simple KEY=VALUE file loaded without printing secret values.",
    )
    parser.add_argument(
        "--timezone", default="Asia/Shanghai", help="Timezone for relative Timeline cases."
    )
    parser.add_argument(
        "--route-retries",
        type=int,
        default=1,
        choices=range(0, 4),
        metavar="N",
        help="Retry only transport/route exhaustion N times; content failures are never retried.",
    )
    parser.add_argument(
        "--delay-ms",
        type=int,
        default=6000,
        help="Delay between cases to avoid turning acceptance into a provider burst test.",
    )
    parser.add_argument(
        "--case",
        action="append",
        choices=_all_case_ids(),
        help="Run only the selected case. Repeat to select multiple cases.",
    )
    parser.add_argument(
        "--exclude-case",
        action="append",
        choices=_all_case_ids(),
        help="Exclude a case from the default/full matrix. Repeat as needed.",
    )
    parser.add_argument("--report", help="Optional JSON report output path.")
    parser.add_argument("--list", action="store_true", help="List case IDs without model calls.")
    args = parser.parse_args()
    if args.list:
        for case_id in _all_case_ids():
            print(case_id)
        return
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
