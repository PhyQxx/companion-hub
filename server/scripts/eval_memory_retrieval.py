"""记忆检索评测考题集与离线跑分（方案 B2）。

合成语料 + 带期望答案的考题，度量 recall@8 / Top-1 / grounded 命中与延迟；
不依赖真实模型，结果 JSON 供影子对比与调参决策留档。任何打分或召回
规则的修改都应先用本脚本跑出前后对比，再决定是否合入。

用法：
    uv run python scripts/eval_memory_retrieval.py --backend sqlite \
        --out docs/baselines/memory-retrieval-eval.json [--fill 0]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from app.db import AppUserRecord, Database
from app.ids import uuid7
from app.memory import RETRIEVAL_POLICY_VERSION, MemoryRetriever, MemoryStore
from app.memory.lifecycle import MemoryLifecycle
from app.memory.lifecycle_policy import MemoryTier
from app.memory.models import (
    MemoryCandidate,
    MemoryOriginKind,
    MemorySourceKind,
    MemorySourceRef,
    MemoryStatus,
    MemorySubjectKind,
    MemoryType,
)
from app.schemas.common import PrivacyLevel
from scripts.benchmark_storage import open_storage


async def _new_user(database: Database, display_name: str) -> UUID:
    user_id = uuid7()
    async with database.sessions.begin() as session:
        session.add(
            AppUserRecord(
                id=user_id, display_name=display_name, status="active", timezone="Asia/Shanghai"
            )
        )
    return user_id


@dataclass(frozen=True, slots=True)
class EvalCase:
    """一道检索考题：问句 + 期望/禁止命中的语料标签。"""

    query: str
    privacy_level: PrivacyLevel
    expected: tuple[str, ...]
    category: str
    top1: str | None = None
    forbidden: tuple[str, ...] = ()
    deep: bool = False


@dataclass(slots=True)
class CorpusResult:
    labels: dict[str, int] = field(default_factory=dict)


def _candidate(
    content: str,
    *,
    type: MemoryType = MemoryType.SEMANTIC,
    subject: MemorySubjectKind = MemorySubjectKind.USER,
    subject_key: str | None = None,
    fact_key: str | None = None,
    privacy: PrivacyLevel = PrivacyLevel.L1,
    importance: float = 0.6,
    initial_tier: str | None = None,
) -> MemoryCandidate:
    if subject is MemorySubjectKind.USER:
        key = subject_key or "user:self"
    elif subject is MemorySubjectKind.ASSISTANT:
        key = subject_key or "assistant:primary"
    else:
        key = subject_key or "shared:user-assistant"
    return MemoryCandidate(
        subject_kind=subject,
        subject_key=key,
        fact_key=fact_key,
        origin_kind=MemoryOriginKind.USER_STATEMENT,
        type=type,
        content=content,
        privacy_level=privacy,
        sources=[MemorySourceRef(source_kind=MemorySourceKind.MANUAL, source_id="eval:1")],
        importance=importance,
        confidence=0.9,
        initial_tier=initial_tier,
        extractor_version="eval-corpus",
        summary=None,
        pin=False,
    )


# 语料：覆盖六级等级、五类记忆、三主体、隐私 L0-L2 与事实槽位。
# 元组：(标签, 内容, kwargs)；dormant 等级在入库后经 set_tier 显式设置。
CORPUS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    (
        "daughter_birthday",
        "用户的女儿小晴生日是2024年5月6日",
        {"fact_key": "person.小晴.birthday", "initial_tier": "core", "importance": 0.9},
    ),
    (
        "son_birthday",
        "用户的儿子乐乐生日是2020年3月12日",
        {"fact_key": "person.乐乐.birthday", "importance": 0.8},
    ),
    (
        "own_birthday",
        "用户的生日是1990年11月20日",
        {"fact_key": "profile.birthday", "importance": 0.8},
    ),
    ("city", "用户住在青岛，市南区", {"fact_key": "profile.city", "importance": 0.7}),
    ("job", "用户在IT行业工作，担任ELN产品负责人", {"fact_key": "profile.job", "importance": 0.8}),
    ("pet", "用户养了一只橘猫，名字叫年糕", {"fact_key": "profile.pet", "importance": 0.7}),
    ("nickname", "用户的小名叫小雨", {"fact_key": "profile.nickname", "importance": 0.5}),
    ("sport", "用户每周晨跑三次，通常在六点起床", {"fact_key": "routine.sport", "importance": 0.7}),
    (
        "food_spice",
        "用户喜欢吃辣，尤其爱水煮鱼",
        {"fact_key": "preference.food", "importance": 0.7},
    ),
    (
        "food_cilantro",
        "用户不吃香菜，点菜时需要去掉香菜",
        {"fact_key": "preference.food", "importance": 0.6},
    ),
    (
        "drink",
        "用户喜欢喝手冲咖啡，不爱喝速溶",
        {"fact_key": "preference.drink", "importance": 0.6},
    ),
    ("height", "用户身高172厘米", {"fact_key": "profile.height", "importance": 0.5}),
    ("weight", "用户体重68公斤", {"fact_key": "profile.weight", "importance": 0.4}),
    ("schedule", "用户工作日晚睡，通常零点半休息", {"importance": 0.5}),
    ("piano", "用户周末学习钢琴，已经坚持两年", {"importance": 0.6}),
    ("allergy", "用户对芒果过敏，不能吃芒果", {"importance": 0.8, "initial_tier": "important"}),
    ("mother_job", "用户的母亲是退休教师，喜欢园艺", {"importance": 0.6}),
    (
        "anniversary",
        "用户和伴侣的结婚纪念日是每年6月18日",
        {"type": MemoryType.EMOTIONAL, "importance": 0.9, "initial_tier": "important"},
    ),
    (
        "commitment_water",
        "用户答应伴侣每天晚上提醒她喝水",
        {"type": MemoryType.COMMITMENT, "importance": 0.7},
    ),
    (
        "commitment_report",
        "用户委托助手每周五整理一份工作周报",
        {"type": MemoryType.COMMITMENT, "importance": 0.8},
    ),
    (
        "episodic_trip",
        "用户上个月和家人去崂山爬山，全程很开心",
        {"type": MemoryType.EPISODIC, "importance": 0.6},
    ),
    (
        "episodic_interview",
        "用户上周完成了ELN产品的年度评审汇报",
        {"type": MemoryType.EPISODIC, "importance": 0.7},
    ),
    (
        "shared_goal",
        "用户和助手约定三个月内完成钢琴二级备考",
        {"type": MemoryType.COMMITMENT, "subject": MemorySubjectKind.SHARED, "importance": 0.7},
    ),
    (
        "assistant_height",
        "助手的身高是160厘米",
        {"subject": MemorySubjectKind.ASSISTANT, "fact_key": "profile.height", "importance": 0.6},
    ),
    (
        "assistant_birthday",
        "助手的生日是4月19日",
        {"subject": MemorySubjectKind.ASSISTANT, "fact_key": "profile.birthday", "importance": 0.6},
    ),
    (
        "assistant_favorite",
        "助手最喜欢的水果是草莓",
        {"subject": MemorySubjectKind.ASSISTANT, "importance": 0.5},
    ),
    (
        "shared_anniversary",
        "用户与助手相识一周年是2026年10月1日",
        {"type": MemoryType.EMOTIONAL, "subject": MemorySubjectKind.SHARED, "importance": 0.6},
    ),
    (
        "l2_video_call",
        "用户昨晚和伴侣视频通话了两小时，情绪愉快",
        {"type": MemoryType.EPISODIC, "privacy": PrivacyLevel.L2, "importance": 0.5},
    ),
    (
        "l2_anniversary_plan",
        "用户计划和伴侣在下个月安排一次两人旅行",
        {"type": MemoryType.COMMITMENT, "privacy": PrivacyLevel.L2, "importance": 0.6},
    ),
    ("l0_public", "用户常去的图书馆是市图书馆", {"privacy": PrivacyLevel.L0, "importance": 0.3}),
    # 干扰项：与多数考题无关但等级/重要性高，用于验证“等级不制造相关性”。
    (
        "distractor_space",
        "用户喜欢研究合成航天器的发展历史",
        {"importance": 0.9, "initial_tier": "core"},
    ),
    (
        "distractor_quantum",
        "用户对量子物理科普视频很感兴趣",
        {"importance": 0.9, "initial_tier": "core"},
    ),
    (
        "distractor_stocks",
        "用户关注新能源板块的股票行情",
        {"importance": 0.8, "initial_tier": "important"},
    ),
    # 冷层成员：入库后显式降为 dormant / low。
    ("dormant_project", "用户曾经参与过社区合唱团，唱男低音", {"importance": 0.4}),
    ("dormant_game", "用户以前常玩桌游狼人杀", {"importance": 0.3}),
    ("low_plant", "用户阳台上养了一盆绿萝", {"importance": 0.3}),
)

DORMANT_LABELS = {"dormant_project", "dormant_game"}
LOW_LABELS = {"low_plant"}


def _cases() -> list[EvalCase]:
    cases: list[EvalCase] = []
    # 1. 冷层精确槽位：dormant/low 记忆经 fact_key 槽位仍可命中。
    cases += [
        EvalCase(
            "小晴的生日是哪天",
            PrivacyLevel.L1,
            ("daughter_birthday",),
            "exact_cold",
            top1="daughter_birthday",
        ),
        EvalCase(
            "乐乐的出生日期是什么时候",
            PrivacyLevel.L1,
            ("son_birthday",),
            "exact_cold",
            top1="son_birthday",
        ),
        EvalCase(
            "我的生日是哪天", PrivacyLevel.L1, ("own_birthday",), "exact_cold", top1="own_birthday"
        ),
        EvalCase("用户住在哪座城市", PrivacyLevel.L1, ("city",), "exact_cold", top1="city"),
        EvalCase("用户做什么工作", PrivacyLevel.L1, ("job",), "exact_cold", top1="job"),
        EvalCase("用户养的猫叫什么名字", PrivacyLevel.L1, ("pet",), "exact_cold", top1="pet"),
        EvalCase("用户平时怎么锻炼身体", PrivacyLevel.L1, ("sport",), "exact_cold", top1="sport"),
        EvalCase("用户身高多高", PrivacyLevel.L1, ("height",), "exact_cold", top1="height"),
        EvalCase("用户体重多重", PrivacyLevel.L1, ("weight",), "exact_cold", top1="weight"),
        EvalCase(
            "用户饮食上有什么偏好",
            PrivacyLevel.L1,
            ("food_spice", "food_cilantro", "allergy"),
            "exact_cold",
        ),
        EvalCase("用户喜欢喝什么饮料", PrivacyLevel.L1, ("drink",), "exact_cold", top1="drink"),
        EvalCase("用户的小名是什么", PrivacyLevel.L1, ("nickname",), "exact_cold", top1="nickname"),
    ]
    # 2. 语义/词法改写：无槽位提示词，靠向量+词法通道。
    cases += [
        EvalCase("家里那只橘猫叫什么来着", PrivacyLevel.L1, ("pet",), "lexical_rewrite"),
        EvalCase("用户在哪个城市安家", PrivacyLevel.L1, ("city",), "lexical_rewrite"),
        EvalCase("用户干哪一行的", PrivacyLevel.L1, ("job",), "lexical_rewrite"),
        EvalCase(
            "用户对什么水果过敏", PrivacyLevel.L1, ("allergy",), "lexical_rewrite", top1="allergy"
        ),
        EvalCase("用户妈妈退休前做什么的", PrivacyLevel.L1, ("mother_job",), "lexical_rewrite"),
        EvalCase("用户周末有什么爱好活动", PrivacyLevel.L1, ("piano", "sport"), "lexical_rewrite"),
        EvalCase("用户平时几点休息", PrivacyLevel.L1, ("schedule",), "lexical_rewrite"),
        EvalCase(
            "用户上个月有什么开心的经历", PrivacyLevel.L1, ("episodic_trip",), "lexical_rewrite"
        ),
    ]
    # 3. 干扰项在场：无关核心不得挤掉相关答案（相关性先于等级）。
    cases += [
        EvalCase(
            "点菜时能吃香菜吗",
            PrivacyLevel.L1,
            ("food_cilantro",),
            "distractor",
            top1="food_cilantro",
            forbidden=("distractor_space", "distractor_quantum", "distractor_stocks"),
        ),
        EvalCase(
            "用户爱吃辣吗",
            PrivacyLevel.L1,
            ("food_spice",),
            "distractor",
            top1="food_spice",
            forbidden=("distractor_space", "distractor_quantum"),
        ),
        EvalCase(
            "用户的结婚纪念日是哪天",
            PrivacyLevel.L1,
            ("anniversary",),
            "distractor",
            top1="anniversary",
            forbidden=("distractor_stocks",),
        ),
        EvalCase(
            "用户答应过伴侣什么事情",
            PrivacyLevel.L1,
            ("commitment_water",),
            "distractor",
            top1="commitment_water",
            forbidden=("distractor_quantum",),
        ),
        EvalCase(
            "助手每周五要帮用户做什么",
            PrivacyLevel.L1,
            ("commitment_report",),
            "distractor",
            top1="commitment_report",
            forbidden=("distractor_space",),
        ),
        EvalCase(
            "用户上周的工作汇报顺利吗",
            PrivacyLevel.L1,
            ("episodic_interview",),
            "distractor",
            top1="episodic_interview",
            forbidden=("distractor_stocks",),
        ),
        EvalCase(
            "用户对芒果是什么态度",
            PrivacyLevel.L1,
            ("allergy",),
            "distractor",
            top1="allergy",
            forbidden=("distractor_quantum", "distractor_space"),
        ),
        EvalCase(
            "用户常去哪里借书",
            PrivacyLevel.L1,
            ("l0_public",),
            "distractor",
            top1="l0_public",
            forbidden=("distractor_space",),
        ),
    ]
    # 4. 盘点型：契约是非空且主体正确，不要求特定成员进入前 8。
    cases += [
        EvalCase("关于我的记忆有哪些", PrivacyLevel.L1, ("daughter_birthday",), "inventory"),
        EvalCase("你都记得我什么信息", PrivacyLevel.L1, ("daughter_birthday",), "inventory"),
        EvalCase(
            "关于你自己的记忆有什么",
            PrivacyLevel.L1,
            (),
            "inventory",
            forbidden=("daughter_birthday", "city"),
        ),
    ]
    # 5. 隐私边界：L2 内容对 L1 查询绝对不可见，对 L2 查询可见。
    cases += [
        EvalCase(
            "用户昨晚和伴侣做了什么",
            PrivacyLevel.L1,
            (),
            "privacy_boundary",
            forbidden=("l2_video_call", "l2_anniversary_plan"),
        ),
        EvalCase(
            "用户有和伴侣有关的安排吗",
            PrivacyLevel.L1,
            (),
            "privacy_boundary",
            forbidden=("l2_video_call", "l2_anniversary_plan"),
        ),
        EvalCase(
            "用户昨晚和伴侣做了什么",
            PrivacyLevel.L2,
            ("l2_video_call",),
            "privacy_boundary",
            top1="l2_video_call",
        ),
        EvalCase(
            "用户和伴侣计划了什么两人活动",
            PrivacyLevel.L2,
            ("l2_anniversary_plan",),
            "privacy_boundary",
            top1="l2_anniversary_plan",
        ),
    ]
    # 6. 多事实与跨主体。
    cases += [
        EvalCase(
            "用户的作息和运动习惯是什么样的", PrivacyLevel.L1, ("sport", "schedule"), "multi_fact"
        ),
        EvalCase("用户家里的宠物和绿植有哪些", PrivacyLevel.L1, ("pet",), "multi_fact"),
        EvalCase(
            "我们之间有什么约定",
            PrivacyLevel.L1,
            ("shared_goal", "shared_anniversary"),
            "cross_subject",
        ),
        EvalCase(
            "你多高",
            PrivacyLevel.L1,
            ("assistant_height",),
            "cross_subject",
            top1="assistant_height",
        ),
        EvalCase(
            "你的生日是哪天",
            PrivacyLevel.L1,
            ("assistant_birthday",),
            "cross_subject",
            top1="assistant_birthday",
        ),
        EvalCase(
            "你最喜欢吃什么水果",
            PrivacyLevel.L1,
            ("assistant_favorite",),
            "cross_subject",
            top1="assistant_favorite",
        ),
    ]
    # 7. 冷层非槽位：设计契约是深搜/强词面扩展可命中，普通模式不作保证。
    cases += [
        EvalCase(
            "用户以前玩过狼人杀吗", PrivacyLevel.L1, ("dormant_game",), "cold_deep", deep=True
        ),
        EvalCase(
            "用户在合唱团唱什么声部", PrivacyLevel.L1, ("dormant_project",), "cold_deep", deep=True
        ),
        EvalCase("用户阳台养了什么植物", PrivacyLevel.L1, ("low_plant",), "cold_deep", deep=True),
    ]
    return cases


async def build_corpus(store: MemoryStore, owner: UUID) -> CorpusResult:
    result = CorpusResult()
    for label, content, kwargs in CORPUS:
        entry = await store.add(_candidate(content, **kwargs), user_id=owner, tier_reason="manual")
        result.labels[label] = entry.id
    lifecycle = MemoryLifecycle(store.database)
    for label in DORMANT_LABELS:
        await lifecycle.set_tier(
            result.labels[label],
            user_id=owner,
            tier=MemoryTier.DORMANT,
            locked=False,
            expected_version=0,
        )
    for label in LOW_LABELS:
        await lifecycle.set_tier(
            result.labels[label],
            user_id=owner,
            tier=MemoryTier.LOW,
            locked=False,
            expected_version=0,
        )
    return result


async def add_fillers(store: MemoryStore, owner: UUID, count: int) -> None:
    """合成填充记忆：只用于放大候选池规模测延迟，不参与考题期望。"""
    if count <= 0:
        return
    marker = uuid7().hex[:6]
    for index in range(count):
        await store.add(
            _candidate(
                f"合成填充条目{marker}{index}：编号{index}的无关描述性事实，仅用于容量评估",
                importance=0.3,
            ),
            user_id=owner,
        )


async def run_eval(database: Database, *, fill: int = 0) -> dict[str, Any]:
    owner = await _new_user(database, "eval-owner")
    foreign = await _new_user(database, "eval-foreign")
    store = MemoryStore(database)
    corpus = await build_corpus(store, owner)
    # 跨用户隔离语料：别人的记忆绝不能出现在本用户的任何命中里。
    corpus.labels["foreign_city"] = (
        await store.add(
            _candidate("用户住在成都，高新区", fact_key="profile.city"), user_id=foreign
        )
    ).id
    corpus.labels["foreign_pet"] = (
        await store.add(_candidate("用户养了一只狗，名字叫豆豆"), user_id=foreign)
    ).id
    await add_fillers(store, owner, fill)

    retriever = MemoryRetriever(store)
    cases = _cases()
    per_category: dict[str, dict[str, float]] = {}
    privacy_violations = 0
    distractor_intrusions = 0
    cross_user_leaks = 0
    violations: list[dict[str, object]] = []
    latencies: list[float] = []
    for case in cases:
        started = time.perf_counter()
        result = await retriever.retrieve(
            case.query, user_id=owner, privacy_level=case.privacy_level, deep=case.deep
        )
        latencies.append((time.perf_counter() - started) * 1000.0)
        hit_ids = [hit.memory.id for hit in result.hits]
        expected_ids = {corpus.labels[label] for label in case.expected}
        hit_forbidden = sorted(label for label in case.forbidden if corpus.labels[label] in hit_ids)
        if hit_forbidden:
            detail: dict[str, object] = {
                "query": case.query,
                "category": case.category,
                "hit_forbidden": hit_forbidden,
            }
            violations.append(detail)
            # 隐私契约（L2 对 L1 查询泄漏）是硬失败；
            # 干扰项进入宽松 Top-8 属于质量信号，单独计数。
            if case.category == "privacy_boundary":
                privacy_violations += 1
            else:
                distractor_intrusions += 1
        if any(hit.memory.user_id != owner for hit in result.hits):
            cross_user_leaks += 1
        recall = len(set(hit_ids[:8]) & expected_ids) / len(expected_ids) if expected_ids else 1.0
        if case.deep and expected_ids:
            # 冷层非槽位的设计契约是深搜候选池覆盖全等级（排序仍适用），
            # 不保证挤进 Top-8：按池覆盖计量，而非最终命中。
            from datetime import UTC
            from datetime import datetime as _datetime

            from app.memory.retrieval import DEFAULT_SUBJECT_SCOPES
            from app.schemas.common import persistent_privacy_levels

            moment = _datetime.now(UTC)
            pool = await store.retrieval_candidates(
                owner,
                subject_scopes=DEFAULT_SUBJECT_SCOPES,
                statuses=(MemoryStatus.ACTIVE,),
                privacy_levels=persistent_privacy_levels(case.privacy_level),
                valid_at=moment,
                limit=500,
                query_text=case.query,
                deep=True,
            )
            recall = len({item.entry.id for item in pool} & expected_ids) / len(expected_ids)
        grounded = retriever.grounded_hits(result)
        grounded_ids = {hit.memory.id for hit in grounded}
        grounded_recall = (
            len(grounded_ids & expected_ids) / len(expected_ids) if expected_ids else 1.0
        )
        top1_ok = (
            1.0
            if case.top1 is None
            else float(bool(hit_ids) and hit_ids[0] == corpus.labels[case.top1])
        )
        bucket = per_category.setdefault(
            case.category,
            {
                "cases": 0.0,
                "recall_at_8": 0.0,
                "grounded_recall": 0.0,
                "top1": 0.0,
                "top1_cases": 0.0,
            },
        )
        bucket["cases"] += 1
        bucket["recall_at_8"] += recall
        bucket["grounded_recall"] += grounded_recall
        if case.top1 is not None:
            bucket["top1"] += top1_ok
            bucket["top1_cases"] += 1
    for bucket in per_category.values():
        count = bucket["cases"]
        bucket["recall_at_8"] = round(bucket["recall_at_8"] / count, 4)
        bucket["grounded_recall"] = round(bucket["grounded_recall"] / count, 4)
        if bucket["top1_cases"]:
            bucket["top1"] = round(bucket["top1"] / bucket["top1_cases"], 4)
        else:
            bucket.pop("top1")
            bucket.pop("top1_cases")
    total = len(cases)
    latencies.sort()

    def percentile(fraction: float) -> float:
        return latencies[min(int(fraction * (total - 1)), total - 1)]

    return {
        "cases": total,
        "corpus_memories": len(corpus.labels) - 2,
        "fill": fill,
        "per_category": per_category,
        "overall": {
            "recall_at_8": round(
                sum(b["recall_at_8"] * b["cases"] for b in per_category.values()) / total, 4
            ),
            "grounded_recall": round(
                sum(b["grounded_recall"] * b["cases"] for b in per_category.values()) / total, 4
            ),
            "privacy_violations": privacy_violations,
            "distractor_intrusions": distractor_intrusions,
            "cross_user_leaks": cross_user_leaks,
        },
        "violations": violations,
        "latency_ms": {
            "p50": round(percentile(0.50), 3),
            "p95": round(percentile(0.95), 3),
            "max": round(latencies[-1], 3),
        },
        "retrieval_policy_version": RETRIEVAL_POLICY_VERSION,
    }


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["sqlite", "postgresql"], default="sqlite")
    parser.add_argument(
        "--out", type=Path, default=Path("docs/baselines/memory-retrieval-eval.json")
    )
    parser.add_argument("--fill", type=int, default=0, help="合成填充记忆数量（测容量延迟）")
    arguments = parser.parse_args()
    url = os.getenv("ARIA_TEST_DATABASE_URL") if arguments.backend == "postgresql" else None
    if arguments.backend == "postgresql" and url is None:
        raise SystemExit("ARIA_TEST_DATABASE_URL is not configured")
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        storage = await open_storage(Path(folder) / "eval.db", url)
        try:
            report = await run_eval(storage.database, fill=arguments.fill)
        finally:
            await storage.close()
    report["backend"] = arguments.backend
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
