# ruff: noqa: RUF001
"""记忆回归评估执行器（docs/03 §1.8 最小版）。

在同一套确定性基础设施（sqlite 内存库 + 哈希嵌入 + 规则检索）上运行
cases.py 的评估集，阈值对齐 docs/01 Release Criteria 第 2~4 条：

- 正例：20 条口语化提问，证据级召回 ≥ 16（80%）；
- 负例：20 条无关 / 过期 / 已替换提问，错误引用 ≤ 1；
- 冲突：同槽位新旧值必须显式裁决，裁决前不静默换值，裁决后只剩胜者；
- 删除：硬删除后自然语言与 fact_key 精确通道都不可再现，台账留痕；
- 隔离：L2 不进 L1 检索、L3 拒绝入库、跨用户零串线。

端到端真实模型链路由 `make p5-real` 覆盖；本套件守住检索/沉淀/删除
这些不依赖模型的确定性语义，两者共同构成 §1.8 的回归集。
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.db import AppUserRecord, Base, Database, create_database
from app.ids import uuid7
from app.memory import (
    ConsolidateDecision,
    MemoryCandidate,
    MemoryIngester,
    MemoryOriginKind,
    MemoryRetriever,
    MemorySourceKind,
    MemorySourceRef,
    MemoryStatus,
    MemoryStore,
    MemorySubjectKind,
    MemoryType,
)
from app.schemas import PrivacyLevel

from .cases import (
    CONFLICT_CASES,
    DELETION_CASES,
    NEGATIVE_CASES,
    POSITIVE_CASES,
    ConflictCase,
    DeletionCase,
    PositiveCase,
)

# 检索侧冻结时钟必须晚于真实墙钟：store.add 用当前时间落 valid_from，
# 若冻结点已过，valid_from <= now 过滤会排除全部新记忆导致评估随时间腐烂
NOW = datetime(2099, 1, 1, tzinfo=UTC)

# Release Criteria 阈值
POSITIVE_MIN_PASS = 16
NEGATIVE_MAX_ERROR = 1

SUBJECT_KEYS: dict[str, tuple[MemorySubjectKind, str]] = {
    "user": (MemorySubjectKind.USER, "user:self"),
    "assistant": (MemorySubjectKind.ASSISTANT, "assistant:primary"),
    "shared": (MemorySubjectKind.SHARED, "shared:user-assistant"),
}

# 负例「已替换」组的新值：旧值必须从检索里消失，只剩新值
SUPERSEDED_REPLACEMENTS: dict[str, str] = {
    "neg-sup-old-city": "用户生活在杭州",
    "neg-sup-old-job": "用户是后端工程师",
    "neg-sup-old-height": "助手身高为 165 厘米",
    "neg-sup-old-nickname": "助手喜欢被叫做小艾",
    "neg-sup-old-coffee": "用户只喝美式咖啡，不加糖",
    "neg-sup-old-wakeup": "用户工作日早上七点起床",
}


@dataclass(frozen=True, slots=True)
class CaseVerdict:
    case_id: str
    passed: bool
    detail: str


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    result = create_database("sqlite+aiosqlite:///:memory:")
    async with result.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield result
    finally:
        await result.close()


@pytest.fixture
async def user(database: Database) -> AppUserRecord:
    record = AppUserRecord(id=uuid7(), display_name="Eval", status="active")
    async with database.sessions.begin() as session:
        session.add(record)
    return record


@pytest.fixture
def store(database: Database) -> MemoryStore:
    return MemoryStore(database)


def _seed_candidate(case: PositiveCase) -> MemoryCandidate:
    subject_kind, subject_key = SUBJECT_KEYS[case.subject]
    return MemoryCandidate(
        type=MemoryType.SEMANTIC,
        content=case.content,
        privacy_level=PrivacyLevel.L1,
        subject_kind=subject_kind,
        subject_key=subject_key,
        fact_key=case.fact_key,
        origin_kind=MemoryOriginKind.USER_STATEMENT,
        sources=[
            MemorySourceRef(
                source_kind=MemorySourceKind.MANUAL, source_id=f"memory-eval:{case.case_id}"
            )
        ],
        importance=0.7,
        confidence=0.9,
        extractor_version="memory-eval-v1",
    )


async def _grounded_contents(
    retriever: MemoryRetriever,
    store: MemoryStore,
    *,
    user_id: UUID,
    query: str,
    privacy_level: PrivacyLevel = PrivacyLevel.L1,
) -> list[str]:
    result = await retriever.retrieve(
        query, user_id=user_id, privacy_level=privacy_level, now=NOW
    )
    return [hit.memory.content for hit in retriever.grounded_hits(result)]


def _report(title: str, verdicts: list[CaseVerdict]) -> None:
    failed = [verdict for verdict in verdicts if not verdict.passed]
    print(f"\n=== 记忆评估 · {title}：{len(verdicts) - len(failed)}/{len(verdicts)} 通过 ===")
    for verdict in failed:
        print(f"  [FAIL] {verdict.case_id}: {verdict.detail}")


async def test_positive_recall_meets_release_criteria(
    store: MemoryStore, user: AppUserRecord
) -> None:
    """正例：口语化提问下证据级召回率 ≥ 80%。"""
    retriever = MemoryRetriever(store)
    for case in POSITIVE_CASES:
        await store.add(_seed_candidate(case), user_id=user.id)

    verdicts: list[CaseVerdict] = []
    for case in POSITIVE_CASES:
        contents = await _grounded_contents(
            retriever, store, user_id=user.id, query=case.query
        )
        hit = any(case.expect_term in content for content in contents)
        verdicts.append(
            CaseVerdict(
                case.case_id,
                hit,
                f"期望术语 {case.expect_term!r} 未出现在证据级命中：{contents}",
            )
        )

    _report("正例召回", verdicts)
    passed = sum(verdict.passed for verdict in verdicts)
    assert passed >= POSITIVE_MIN_PASS, (
        f"正例召回 {passed}/{len(POSITIVE_CASES)} 低于 Release Criteria 的 "
        f"{POSITIVE_MIN_PASS}（80%）"
    )


async def test_negative_false_reference_within_budget(
    store: MemoryStore, user: AppUserRecord
) -> None:
    """负例：无关 / 过期 / 已替换内容不得被当作证据引用。"""
    retriever = MemoryRetriever(store)
    # 正例语料作为干扰项：无关提问必须做到「有东西可错引也不引」
    for case in POSITIVE_CASES:
        await store.add(_seed_candidate(case), user_id=user.id)

    for negative in NEGATIVE_CASES:
        if negative.kind == "expired":
            await store.add(
                MemoryCandidate(
                    type=MemoryType.SEMANTIC,
                    content=negative.content,
                    privacy_level=PrivacyLevel.L1,
                    sources=[
                        MemorySourceRef(
                            source_kind=MemorySourceKind.MANUAL,
                            source_id=f"memory-eval:{negative.case_id}",
                        )
                    ],
                    importance=0.7,
                    valid_to=NOW - timedelta(days=1),
                ),
                user_id=user.id,
            )
        elif negative.kind == "superseded":
            old = await store.add(
                MemoryCandidate(
                    type=MemoryType.SEMANTIC,
                    content=negative.content,
                    privacy_level=PrivacyLevel.L1,
                    sources=[
                        MemorySourceRef(
                            source_kind=MemorySourceKind.MANUAL,
                            source_id=f"memory-eval:{negative.case_id}",
                        )
                    ],
                    importance=0.7,
                ),
                user_id=user.id,
            )
            await store.edit(
                old.id,
                content=SUPERSEDED_REPLACEMENTS[negative.case_id],
                actor="memory-eval",
                reason="负例评估：替换旧值",
            )

    verdicts: list[CaseVerdict] = []
    for negative in NEGATIVE_CASES:
        contents = await _grounded_contents(
            retriever, store, user_id=user.id, query=negative.query
        )
        wrong = any(negative.forbidden_term in content for content in contents)
        verdicts.append(
            CaseVerdict(
                negative.case_id,
                not wrong,
                f"错误引用：{negative.forbidden_term!r} 出现在证据级命中：{contents}",
            )
        )

    _report("负例误引", verdicts)
    errors = sum(not verdict.passed for verdict in verdicts)
    assert errors <= NEGATIVE_MAX_ERROR, (
        f"负例错误引用 {errors}/{len(NEGATIVE_CASES)} 超出 Release Criteria 的 "
        f"{NEGATIVE_MAX_ERROR}"
    )


def _conflict_subject(case: ConflictCase) -> str:
    if case.fact_key.startswith("shared."):
        return "shared"
    if case.fact_key.startswith(("profile.", "preference.")):
        return "assistant"
    return "user"


def _conflict_candidate(case: ConflictCase, value: str) -> MemoryCandidate:
    subject_kind, subject_key = SUBJECT_KEYS[_conflict_subject(case)]
    return MemoryCandidate(
        type=MemoryType.SEMANTIC,
        content=value,
        privacy_level=PrivacyLevel.L1,
        subject_kind=subject_kind,
        subject_key=subject_key,
        fact_key=case.fact_key,
        origin_kind=MemoryOriginKind.USER_STATEMENT,
        sources=[
            MemorySourceRef(
                source_kind=MemorySourceKind.MANUAL,
                source_id=f"memory-eval:{case.case_id}",
            )
        ],
        importance=0.7,
    )


async def test_conflict_requires_adjudication_and_only_winner_survives(
    store: MemoryStore, user: AppUserRecord
) -> None:
    """冲突：裁决前保留旧值挂起新值，裁决后检索只剩胜出方。"""
    retriever = MemoryRetriever(store)
    ingester = MemoryIngester(store)
    verdicts: list[CaseVerdict] = []

    for index, case in enumerate(CONFLICT_CASES):
        assert isinstance(case, ConflictCase)

        await store.add(_conflict_candidate(case, case.old_value), user_id=user.id)
        outcome = await ingester.ingest(_conflict_candidate(case, case.new_value), user_id=user.id)

        pending_ok = outcome.decision is ConsolidateDecision.CONFLICT
        pending_ok = pending_ok and outcome.memory.status == MemoryStatus.CONFLICT
        contents_before = await _grounded_contents(
            retriever, store, user_id=user.id, query=case.query
        )
        # 裁决前：旧值仍在（active），新值被挂起，不允许静默换值
        pending_ok = pending_ok and any(
            _term_of(case.old_value) in content for content in contents_before
        )
        verdicts.append(
            CaseVerdict(
                f"{case.case_id}/pending",
                pending_ok,
                f"冲突未正确挂起：decision={outcome.decision} "
                f"status={outcome.memory.status} 命中={contents_before}",
            )
        )

        adopt = index % 2 == 0
        resolved = await store.resolve_conflict(outcome.memory.id, adopt=adopt, actor="memory-eval")
        contents_after = await _grounded_contents(
            retriever, store, user_id=user.id, query=case.query
        )
        winner_term = _term_of(case.new_value if adopt else case.old_value)
        loser_term = _term_of(case.old_value if adopt else case.new_value)
        winner_grounded = any(winner_term in content for content in contents_after)
        loser_gone = not any(loser_term in content for content in contents_after)
        verdicts.append(
            CaseVerdict(
                f"{case.case_id}/{'adopt' if adopt else 'keep'}",
                winner_grounded and loser_gone,
                f"裁决后命中异常（胜者={winner_term!r} 败者={loser_term!r}）：{contents_after}，"
                f"resolved.status={resolved.status}",
            )
        )

    _report("冲突裁决", verdicts)
    assert all(verdict.passed for verdict in verdicts)


async def test_hard_deletion_never_resurfaces(
    store: MemoryStore, user: AppUserRecord
) -> None:
    """删除：硬删除后自然语言与 fact_key 精确通道都查不到，台账留痕。"""
    retriever = MemoryRetriever(store)
    verdicts: list[CaseVerdict] = []

    for case in DELETION_CASES:
        assert isinstance(case, DeletionCase)
        subject = "shared" if case.fact_key.startswith("shared.") else "user"
        subject_kind, subject_key = SUBJECT_KEYS[subject]
        created = await store.add(
            MemoryCandidate(
                type=MemoryType.SEMANTIC,
                content=case.content,
                privacy_level=PrivacyLevel.L1,
                subject_kind=subject_kind,
                subject_key=subject_key,
                fact_key=case.fact_key,
                origin_kind=MemoryOriginKind.USER_STATEMENT,
                sources=[
                    MemorySourceRef(
                        source_kind=MemorySourceKind.MANUAL,
                        source_id=f"memory-eval:{case.case_id}",
                    )
                ],
                importance=0.7,
            ),
            user_id=user.id,
        )
        # 删除前必须可召回，否则「删除后查不到」没有证明力
        before = await _grounded_contents(retriever, store, user_id=user.id, query=case.query)
        assert any(
            case.fact_key is not None and hit for hit in before
        ) or any(case.content[:2] in content for content in before), (
            f"{case.case_id} 删除前即不可召回，用例无效：{before}"
        )

        await store.hard_delete(created.id, actor="memory-eval", reason="负例评估：硬删除")

        contents = await _grounded_contents(retriever, store, user_id=user.id, query=case.query)
        gone = not any(
            case.content[:3] in content or case.content[-3:] in content for content in contents
        )
        exact_left = await store.fact_candidates(
            user_id=user.id,
            fact_key=case.fact_key,
            subject_scopes=tuple(SUBJECT_KEYS.values()),
            privacy_levels=(PrivacyLevel.L0, PrivacyLevel.L1),
            valid_at=NOW,
        )
        verdicts.append(
            CaseVerdict(
                case.case_id,
                gone and not exact_left,
                f"删除后仍可检索：自然语言命中={contents} "
                f"fact_key 残留={[e.id for e in exact_left]}",
            )
        )

    _report("删除不可再现", verdicts)
    assert all(verdict.passed for verdict in verdicts)

    ledger = await store.list_deletion_ledger(limit=50)
    assert len(ledger) >= len(DELETION_CASES), f"删除台账留痕不足：{len(ledger)}"


async def test_privacy_isolation_holds_at_retrieval_layer(
    store: MemoryStore, user: AppUserRecord, database: Database
) -> None:
    """隔离：L2 不进 L1 检索、L3 拒绝入库、跨用户零串线。"""
    retriever = MemoryRetriever(store)

    await store.add(
        MemoryCandidate(
            type=MemoryType.EMOTIONAL,
            content="用户与助手之间有一段私密约定：暗号是北极星",
            privacy_level=PrivacyLevel.L2,
            subject_kind=MemorySubjectKind.USER,
            subject_key="user:self",
            fact_key="user.secret",
            origin_kind=MemoryOriginKind.USER_STATEMENT,
            sources=[
                MemorySourceRef(
                    source_kind=MemorySourceKind.MANUAL, source_id="memory-eval:iso-secret"
                )
            ],
            importance=0.8,
        ),
        user_id=user.id,
    )

    public_contents = await _grounded_contents(
        retriever, store, user_id=user.id, query="我们的暗号是什么？", privacy_level=PrivacyLevel.L1
    )
    assert not any("北极星" in content for content in public_contents), (
        f"L2 记忆泄漏进 L1 检索：{public_contents}"
    )

    private_contents = await _grounded_contents(
        retriever, store, user_id=user.id, query="我们的暗号是什么？", privacy_level=PrivacyLevel.L2
    )
    assert any("北极星" in content for content in private_contents), (
        f"L2 上下文未能召回敏感记忆：{private_contents}"
    )

    with pytest.raises(ValueError, match="L3"):
        await store.add(
            MemoryCandidate(
                type=MemoryType.SEMANTIC,
                content="原始传感器遥测不应入库",
                privacy_level=PrivacyLevel.L3,
                sources=[
                    MemorySourceRef(
                        source_kind=MemorySourceKind.MANUAL, source_id="memory-eval:iso-l3"
                    )
                ],
            ),
            user_id=user.id,
        )

    stranger = AppUserRecord(id=uuid7(), display_name="Stranger", status="active")
    async with database.sessions.begin() as session:
        session.add(stranger)
    stranger_contents = await _grounded_contents(
        retriever,
        store,
        user_id=stranger.id,
        query="我们的暗号是什么？",
        privacy_level=PrivacyLevel.L2,
    )
    assert not any("北极星" in content for content in stranger_contents), (
        f"跨用户记忆串线：{stranger_contents}"
    )
    print("\n=== 记忆评估 · 敏感隔离：4/4 通过（L1 不泄漏 / L2 可召回 / L3 拒绝 / 跨用户隔离）===")


def _term_of(value: str) -> str:
    """从槽位值里取最具区分性的数字或关键词做包含判定。"""
    numbers = re.findall(r"\d+", value)
    if numbers:
        longest: str = max(numbers, key=len)
        return longest
    return value.replace("助手", "").replace("用户", "").strip()
