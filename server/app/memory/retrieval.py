"""混合检索器：双路召回 → 硬过滤 → 归一化重排 → 类型配额 Top-K。

策略版本（当前 hybrid-lifecycle-v8）会随 decision_meta 一起落库，任何打分或
过滤规则的修改都必须 bump 版本号，保证历史回合可追溯当时的检索行为。
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import final
from uuid import UUID

from app.harness.time import utc
from app.schemas.common import PrivacyLevel, persistent_privacy_levels

from .fact_keys import birthday_key
from .lifecycle_policy import TIERS, MemoryTier
from .logfmt import tier_label
from .models import MemoryEntry, MemoryStatus, MemorySubjectKind, MemoryType
from .retrieval_models import MemoryHit as MemoryHit
from .retrieval_models import RetrievalCandidate
from .retrieval_models import RetrievalResult as RetrievalResult
from .retrieval_ports import MemoryRetrievalRepository
from .similarity import cosine_similarity, lexical_cosine, text_tokens

RETRIEVAL_POLICY_VERSION = "hybrid-lifecycle-v8"

DEFAULT_SUBJECT_SCOPES: tuple[tuple[MemorySubjectKind, str], ...] = (
    (MemorySubjectKind.USER, "user:self"),
    (MemorySubjectKind.ASSISTANT, "assistant:primary"),
    (MemorySubjectKind.SHARED, "shared:user-assistant"),
)

DEFAULT_TYPE_QUOTAS: Mapping[str, int] = {
    MemoryType.SEMANTIC.value: 3,
    MemoryType.PREFERENCE.value: 2,
    MemoryType.COMMITMENT.value: 1,
    MemoryType.EPISODIC.value: 2,
    MemoryType.EMOTIONAL.value: 2,
}

# 低置信门槛与生命周期自动晋升的 confidence ≥ 0.6 围栏（lifecycle.py）同界：
# 达不到该把握的记忆渲染时标注“把握较低”，提示模型先承认不确定再请用户确认。
LOW_CONFIDENCE = 0.6


@dataclass(frozen=True, slots=True)
class RetrievalPolicy:
    top_k: int = 8
    recall_k: int = 30
    candidate_limit: int = 500
    quotas: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_TYPE_QUOTAS))
    relevance_weight: float = 0.55
    importance_weight: float = 0.25
    pin_bonus: float = 0.2
    subject_hint_bonus: float = 0.15
    exact_fact_bonus: float = 1.0
    episodic_recency_weight: float = 0.15
    recency_tau_days: float = 30.0
    grounded_lexical_threshold: float = 0.15
    grounded_vector_threshold: float = 0.55
    inventory_k: int = 20
    identity_k: int = 8

    def __post_init__(self) -> None:
        object.__setattr__(self, "quotas", MappingProxyType(dict(self.quotas)))


@final
class MemoryRetriever:
    def __init__(
        self, store: MemoryRetrievalRepository, *, policy: RetrievalPolicy | None = None
    ) -> None:
        self._store = store
        self._policy = policy or RetrievalPolicy()

    async def retrieve(
        self,
        query: str,
        *,
        user_id: UUID,
        privacy_level: PrivacyLevel,
        now: datetime | None = None,
        deep: bool = False,
    ) -> RetrievalResult:
        moment = utc(now or datetime.now(UTC))
        privacy = PrivacyLevel(privacy_level)
        subject_hint = _infer_subject_hint(query)
        fact_hints = _infer_fact_keys(query)
        fact_hint = fact_hints[0] if fact_hints else None
        allowed_levels = persistent_privacy_levels(privacy)
        if not allowed_levels:
            return RetrievalResult(
                hits=(),
                policy_version=RETRIEVAL_POLICY_VERSION,
                candidate_count=0,
                vector_recalled=0,
                lexical_recalled=0,
                subject_hint=subject_hint,
                fact_hint=fact_hint,
            )
        raw_candidates = await self._store.retrieval_candidates(
            user_id,
            subject_scopes=DEFAULT_SUBJECT_SCOPES,
            statuses=(MemoryStatus.ACTIVE,),
            privacy_levels=allowed_levels,
            valid_at=moment,
            limit=self._policy.candidate_limit,
            query_text=query,
            deep=deep,
        )
        candidates = tuple(
            RetrievalCandidate(
                entry=item.entry,
                embedding=list(item.embedding) if item.embedding is not None else None,
            )
            for item in raw_candidates
            if _eligible_memory(
                item.entry, user_id=user_id, privacy_levels=allowed_levels, valid_at=moment
            )
        )
        inventory_subject = _infer_inventory_subject(query)
        if inventory_subject is not None:
            inventory_hits = tuple(
                MemoryHit(
                    memory=item.entry,
                    vector_score=0.0,
                    lexical_score=0.0,
                    final_score=item.entry.importance,
                    reasons=("subject_inventory",),
                )
                for item in candidates
                if item.entry.subject_kind == inventory_subject.value
            )[: self._policy.inventory_k]
            await self._store.record_access([hit.memory.id for hit in inventory_hits])
            return RetrievalResult(
                hits=inventory_hits,
                policy_version=RETRIEVAL_POLICY_VERSION,
                candidate_count=len(candidates),
                vector_recalled=0,
                lexical_recalled=0,
                subject_hint=inventory_subject.value,
            )
        provider = self._store.embedding_provider
        provider_version, provider_dimension = provider.version, provider.dimension
        query_vector = tuple((await provider.embed([query]))[0])
        query_tokens = text_tokens(query)

        def similarity_of(item: RetrievalCandidate) -> float:
            if (
                item.embedding is None
                or item.entry.embedding_version != provider_version
                or item.entry.embedding_dimension != provider_dimension
                or len(item.embedding) != provider_dimension
            ):
                return 0.0
            return cosine_similarity(query_vector, item.embedding)

        if self._store.vector_sql_enabled:
            # PostgreSQL 上用 pgvector ANN 替换全量扫描；
            # 两条路径产出同构的 (id, 相似度) 列表，进入同一套合并/重排
            vector_ranked = await self._store.vector_recall(
                query_vector,
                user_id=user_id,
                embedding_version=provider_version,
                privacy_levels=[level.value for level in allowed_levels],
                subject_keys=[key for _, key in DEFAULT_SUBJECT_SCOPES],
                valid_at=moment,
                limit=self._policy.recall_k,
            )
            valid_vector_ids = {
                item.entry.id
                for item in candidates
                if item.embedding is not None
                and item.entry.embedding_version == provider_version
                and item.entry.embedding_dimension == provider_dimension
                and len(item.embedding) == provider_dimension
            }
            vector_ranked = [
                (memory_id, score)
                for memory_id, score in vector_ranked
                if memory_id in valid_vector_ids and math.isfinite(score) and score > 0
            ]
        else:
            vector_ranked = _rank(candidates, similarity_of)
        lexical_ranked = _rank(
            candidates,
            lambda item: lexical_cosine(query_tokens, text_tokens(item.entry.content)),
        )
        vector_top = dict(vector_ranked[: self._policy.recall_k])
        lexical_top = dict(lexical_ranked[: self._policy.recall_k])

        by_id = {item.entry.id: item for item in candidates}
        ranked: list[MemoryHit] = []
        for memory_id in set(vector_top) | set(lexical_top):
            item = by_id.get(memory_id)
            if item is None:
                continue
            entry = item.entry
            vector_score = min(vector_top.get(memory_id, 0.0), 1.0)
            lexical_score = min(lexical_top.get(memory_id, 0.0), 1.0)
            relevance = max(vector_score, lexical_score)
            score = (
                self._policy.relevance_weight * relevance
                + self._policy.importance_weight * entry.importance
            )
            if os.getenv("ARIA_MEMORY_TIERED_RETRIEVAL", "1") == "1":
                score += 0.15 * TIERS.index(MemoryTier(entry.tier)) / (len(TIERS) - 1)
            reasons: list[str] = [f"tier:{entry.tier}"]
            if vector_score > 0:
                reasons.append("vector")
            if lexical_score > 0:
                reasons.append("lexical")
            if entry.pin:
                # 置顶加成：用户手动固定的关键事实优先进入上下文
                score += self._policy.pin_bonus
                reasons.append("pin")
            if subject_hint is not None and entry.subject_kind == subject_hint.value:
                score += self._policy.subject_hint_bonus
                reasons.append("subject_bonus")
            # 新近度只作用于情景记忆：稳定事实（语义/偏好）不随时间衰减
            if entry.type == MemoryType.EPISODIC.value and entry.created_at is not None:
                age_days = max((moment - entry.created_at).total_seconds() / 86_400.0, 0.0)
                score += self._policy.episodic_recency_weight * math.exp(
                    -age_days / self._policy.recency_tau_days
                )
                reasons.append("recency")
            ranked.append(
                MemoryHit(
                    memory=entry,
                    vector_score=vector_score,
                    lexical_score=lexical_score,
                    final_score=score,
                    reasons=tuple(reasons),
                )
            )

        exact_entries: list[MemoryEntry] = []
        for exact_fact_key in fact_hints:
            entries = await self._store.fact_candidates(
                user_id=user_id,
                fact_key=exact_fact_key,
                subject_scopes=DEFAULT_SUBJECT_SCOPES,
                privacy_levels=allowed_levels,
                valid_at=moment,
            )
            exact_entries.extend(
                entry
                for entry in entries
                if entry.fact_key == exact_fact_key
                and _eligible_memory(
                    entry, user_id=user_id, privacy_levels=allowed_levels, valid_at=moment
                )
            )
        if exact_entries:
            ranked_by_id = {hit.memory.id: hit for hit in ranked}
            for entry in exact_entries:
                existing = ranked_by_id.get(entry.id)
                subject_bonus = (
                    self._policy.subject_hint_bonus
                    if subject_hint is not None and entry.subject_kind == subject_hint.value
                    else 0.0
                )
                if existing is not None:
                    exact_reasons = tuple(dict.fromkeys((*existing.reasons, "exact_fact")))
                    ranked_by_id[entry.id] = MemoryHit(
                        memory=entry,
                        vector_score=existing.vector_score,
                        lexical_score=existing.lexical_score,
                        final_score=existing.final_score + self._policy.exact_fact_bonus,
                        reasons=exact_reasons,
                    )
                    continue
                exact_reasons_list = ["exact_fact"]
                if subject_bonus:
                    exact_reasons_list.append("subject_bonus")
                score = (
                    self._policy.exact_fact_bonus
                    + self._policy.importance_weight * entry.importance
                    + subject_bonus
                )
                if entry.pin:
                    score += self._policy.pin_bonus
                    exact_reasons_list.append("pin")
                ranked_by_id[entry.id] = MemoryHit(
                    memory=entry,
                    vector_score=0.0,
                    lexical_score=0.0,
                    final_score=score,
                    reasons=tuple(exact_reasons_list),
                )
            ranked = list(ranked_by_id.values())
        ranked.sort(key=lambda hit: hit.final_score, reverse=True)

        selected: list[MemoryHit] = []
        used: Counter[str] = Counter()
        # 确定性槽位事实先占位，不受普通类型配额挤出。
        for hit in ranked:
            if "exact_fact" not in hit.reasons:
                continue
            selected.append(hit)
            used[hit.memory.type] += 1
            if len(selected) >= self._policy.top_k:
                break
        # 按类型配额截取 Top-K：单一类型不能挤占全部上下文位
        for hit in ranked:
            if hit in selected:
                continue
            quota = self._policy.quotas.get(hit.memory.type)
            if quota is not None and used[hit.memory.type] >= quota:
                continue
            selected.append(hit)
            used[hit.memory.type] += 1
            if len(selected) >= self._policy.top_k:
                break

        # 身份问句兜底（批次九 D1）：“你记得我吗/我是谁”类问题的目标记忆可能
        # 从未打上 fact_key，语义弱命中又过不了证据门槛。正常通道拿不出可作
        # 证据的用户记忆时，按档案优先直取，不能让身份确认被滤成一片空白。
        identity_probe = (
            os.getenv("ARIA_MEMORY_IDENTITY_PROBE", "1") == "1"
            and _infer_identity_probe(query)
            and not any(
                hit.memory.subject_kind == MemorySubjectKind.USER.value
                and (
                    "exact_fact" in hit.reasons
                    or hit.lexical_score >= self._policy.grounded_lexical_threshold
                    or hit.vector_score >= self._policy.grounded_vector_threshold
                )
                for hit in selected
            )
        )
        if identity_probe:
            selected = list(self._identity_probe_hits(candidates))
            _log_identity_probe_summary(selected)
        else:
            _log_retrieval_summary(selected, candidate_count=len(candidates))
        await self._store.record_access([hit.memory.id for hit in selected])
        return RetrievalResult(
            hits=tuple(selected),
            policy_version=RETRIEVAL_POLICY_VERSION,
            candidate_count=len(
                {item.entry.id for item in candidates} | {entry.id for entry in exact_entries}
            ),
            vector_recalled=len(vector_top),
            lexical_recalled=len(lexical_top),
            subject_hint=subject_hint.value if subject_hint is not None else None,
            fact_hint=fact_hint,
            identity_probe=identity_probe,
        )

    def grounded_hits(self, result: RetrievalResult) -> tuple[MemoryHit, ...]:
        """Return only hits strong enough to be treated as remembered evidence.

        Retrieval top-k is intentionally permissive so the model can receive useful
        context. Evidence semantics are stricter: exact fact slots, subject
        inventory and identity probe hits always qualify, lexical matches need a
        minimum overlap, and vector-only matches need a high similarity.
        Importance, pin and subject bonuses may order relevant memories, but must
        never manufacture relevance by themselves.
        """
        return tuple(
            hit
            for hit in result.hits
            if "exact_fact" in hit.reasons
            or "subject_inventory" in hit.reasons
            or "identity_probe" in hit.reasons
            or hit.lexical_score >= self._policy.grounded_lexical_threshold
            or hit.vector_score >= self._policy.grounded_vector_threshold
        )

    def _identity_probe_hits(
        self, candidates: Sequence[RetrievalCandidate]
    ) -> tuple[MemoryHit, ...]:
        """身份问句兜底召回：档案槽优先 → 置顶 → 重要性，不依赖语义相似度。"""
        user_entries = [
            item.entry
            for item in candidates
            if item.entry.subject_kind == MemorySubjectKind.USER.value
        ]
        user_entries.sort(
            key=lambda entry: (
                not (entry.fact_key or "").startswith("profile."),
                not entry.pin,
                -entry.importance,
                -entry.id,
            )
        )
        return tuple(
            MemoryHit(
                memory=entry,
                vector_score=0.0,
                lexical_score=0.0,
                final_score=entry.importance,
                reasons=("identity_probe",),
            )
            for entry in user_entries[: self._policy.identity_k]
        )

    @staticmethod
    def render_context(
        result: RetrievalResult,
        *,
        hits: Sequence[MemoryHit] | None = None,
    ) -> str:
        """按 user / assistant / shared 分组渲染长期记忆，避免主体串线。"""
        selected_hits = tuple(result.hits if hits is None else hits)
        if not selected_hits:
            if result.identity_probe:
                # 批次九 D3：身份问句检索不到记录时，宁可承认“没找到”，
                # 也不允许模型一口咬死“从未记录过”——检索不到 ≠ 没存过。
                return (
                    "【长期记忆】用户正在确认你是否记得TA。当前检索没有可提供的"
                    "用户记录：请如实回答没有找到，不要编造，也不要断言“从未记录过”"
                    "（可能是没检索到，不等于一定没存过）；如本轮提供了记忆查询工具，"
                    "可先按编号或分区核对后再回答。"
                )
            return ""
        labels = {
            MemorySubjectKind.USER.value: "关于用户",
            MemorySubjectKind.ASSISTANT.value: "关于你自己",
            MemorySubjectKind.SHARED.value: "关于你们",
        }
        grouped: dict[str, list[str]] = {key: [] for key in labels}
        low_confidence = False
        for hit in selected_hits:
            fact = f"[{hit.memory.fact_key}]" if hit.memory.fact_key else ""
            line = f"- [{hit.memory.type}]{fact} {hit.memory.content}"
            if hit.memory.summary:
                line += f"（{hit.memory.summary}）"
            if hit.memory.confidence is not None and hit.memory.confidence < LOW_CONFIDENCE:
                line += "（把握较低）"
                low_confidence = True
            grouped.setdefault(hit.memory.subject_kind, []).append(line)
        blocks = [
            "【长期记忆】以下内容来自可追溯的长期记忆。优先遵守 active 的稳定事实；"
            "不要把不同主体混淆。不确定时可以明确说不确定，不要编造。"
        ]
        if any("identity_probe" in hit.reasons for hit in selected_hits):
            blocks.append(
                "用户正在确认你是否记得TA：基于下列关于用户的记录如实回答、逐条概括；"
                "把握较低的先说明并请用户确认，不要直接当作确定事实。"
            )
        if any("subject_inventory" in hit.reasons for hit in selected_hits):
            blocks.append(
                "用户正在要求盘点记忆。请逐条如实概括下列记录；只要下方存在记录，"
                "就不得声称记忆为空，也不要承诺所有对话都会自动成为长期记忆。"
            )
        if low_confidence:
            blocks.append(
                "标注（把握较低）的记录未经用户确认：回答时先说明不确定并请用户核对，"
                "不要当作确定事实引用，也不要因此直接声称没有相关记忆。"
            )
        for subject_kind, label in labels.items():
            lines = grouped.get(subject_kind) or []
            if lines:
                blocks.append(f"[{label}]\n" + "\n".join(lines))
        blocks.append(
            "助手自身记忆用于角色连续性，可以作为身高、体重、三围、生日、偏好等稳定"
            "自我档案直接回答；这类角色自我设定不自动赋予现实动作能力。现实中的移动、"
            "触碰、设备控制或其他实际执行仍以【现实能力边界】为准。"
        )
        return "\n\n".join(blocks)


def _eligible_memory(
    entry: MemoryEntry, *, user_id: UUID, privacy_levels: Sequence[PrivacyLevel], valid_at: datetime
) -> bool:
    return (
        entry.user_id == user_id
        and entry.status == MemoryStatus.ACTIVE.value
        and entry.privacy_level in privacy_levels
        and (entry.subject_kind, entry.subject_key) in DEFAULT_SUBJECT_SCOPES
        and (entry.valid_from is None or utc(entry.valid_from) <= valid_at)
        and (entry.valid_to is None or utc(entry.valid_to) > valid_at)
    )


def _infer_subject_hint(query: str) -> MemorySubjectKind | None:
    normalized = query.strip().lower()
    if any(token in normalized for token in ("我们", "咱们", "咱俩", "我们俩")):
        return MemorySubjectKind.SHARED
    # “你的记忆里关于我的……”中的“你的”修饰“记忆”，真正被询问的主体是用户。
    if any(token in normalized for token in ("关于我", "记得我", "我的工作", "我的职业")):
        return MemorySubjectKind.USER
    if any(token in normalized for token in ("你的", "你自己", "你叫什么", "你多高", "你多重")):
        return MemorySubjectKind.ASSISTANT
    if any(token in normalized for token in ("我的", "我自己", "我叫", "我是不是", "我喜欢")):
        return MemorySubjectKind.USER
    return None


def _infer_fact_keys(query: str) -> tuple[str, ...]:
    normalized = query.strip().lower()
    birthday = birthday_key(query)
    if birthday:
        return (birthday,)
    patterns: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("profile.measurements", ("三围", "身材数据", "身体围度", "胸围腰围臀围")),
        ("profile.height", ("身高", "多高")),
        ("profile.weight", ("体重", "多重")),
        ("profile.birthday", ("生日", "出生日期")),
        ("profile.nickname", ("昵称", "小名")),
        ("profile.name", ("名字", "姓名", "叫什么")),
        ("profile.city", ("住在", "哪座城市", "哪个城市", "在哪生活")),
        (
            "profile.job",
            ("职业", "工作", "做什么工作", "工作是什么", "干什么的"),
        ),
        ("profile.pet", ("养的猫", "猫叫什么", "养的狗", "狗叫什么", "宠物")),
        ("routine.sport", ("运动", "锻炼", "健身")),
        ("preference.food", ("饮食偏好", "喜欢吃", "爱吃", "不吃")),
        ("preference.drink", ("饮料偏好", "喜欢喝", "爱喝")),
    )
    return tuple(
        fact_key for fact_key, tokens in patterns if any(token in normalized for token in tokens)
    )


def _infer_inventory_subject(query: str) -> MemorySubjectKind | None:
    """识别按主体盘点记忆的请求；这类问题不能依赖语义相似度逐条碰运气。"""

    normalized = re.sub(r"\s+", "", query.strip().lower())
    inventory_markers = (
        "有哪些",
        "有什么",
        "记得哪些",
        "都记得",
        "列出",
        "记忆里关于",
        "关于我的记忆",
        "关于你的记忆",
        "记忆里有没有",
        "历史记忆",
        "长期记忆",
        "记得些什么",
    )
    if not any(marker in normalized for marker in inventory_markers):
        return None
    if any(token in normalized for token in ("我们", "咱们", "咱俩", "我们俩")):
        return MemorySubjectKind.SHARED
    # “你的历史记忆/你的记忆”问的是助手库里关于用户的记录，主体仍是用户。
    if any(
        token in normalized
        for token in ("关于我", "我的记忆", "记得我", "你的记忆", "历史记忆", "长期记忆")
    ):
        return MemorySubjectKind.USER
    if any(token in normalized for token in ("关于你", "你自己的记忆")):
        return MemorySubjectKind.ASSISTANT
    return None


_IDENTITY_PROBE_PATTERNS: tuple[str, ...] = (
    "你记得我",
    "还记得我",
    "记得我吗",
    "记得我不",
    "认识我",
    "我是谁",
    "我叫什么",
    "我叫啥",
    "我的名字",
    "知道我是谁",
    "想得起我",
)


def _infer_identity_probe(query: str) -> bool:
    """识别“你是否记得我/我是谁”类身份问句；命中后走兜底直取而非语义碰运气。

    精确槽位问句（如“还记得我的工作吗”）仍优先走 exact_fact 通道：
    探针只在正常检索给不出可作证据的用户记忆时兜底（见 retrieve 尾部）。
    """
    normalized = re.sub(r"\s+", "", query.strip().lower())
    return any(pattern in normalized for pattern in _IDENTITY_PROBE_PATTERNS)


def _rank(
    candidates: Sequence[RetrievalCandidate],
    key: Callable[[RetrievalCandidate], float],
) -> list[tuple[int, float]]:
    ranked = [(item.entry.id, key(item)) for item in candidates]
    ranked = [(memory_id, score) for memory_id, score in ranked if score > 0]
    ranked.sort(key=lambda pair: pair[1], reverse=True)
    return ranked


def _log_retrieval_summary(selected: Sequence[MemoryHit], *, candidate_count: int) -> None:
    """业主可读：本轮对话想起了哪些记忆、什么等级（不含内容）。"""
    if not selected:
        logging.getLogger(__name__).info(
            "memory.story 本轮没有需要想起的长期记忆（候选%d条）", candidate_count
        )
        return
    counts: dict[str, int] = {}
    exact = 0
    for hit in selected:
        counts[hit.memory.tier] = counts.get(hit.memory.tier, 0) + 1
        exact += "exact_fact" in hit.reasons
    distribution = "、".join(
        f"{tier_label(tier)}{count}" for tier, count in sorted(counts.items(), reverse=True)
    )
    logging.getLogger(__name__).info(
        "memory.story 本轮注入%d条长期记忆（等级：%s；精确槽命中%d条）",
        len(selected),
        distribution,
        exact,
    )


def _log_identity_probe_summary(probe_hits: Sequence[MemoryHit]) -> None:
    """业主可读：身份问句兜底召回了哪些等级的记忆（不含内容）。"""
    if not probe_hits:
        logging.getLogger(__name__).info(
            "memory.story 用户在确认你是否记得TA，没有找到可提供的用户记忆"
        )
        return
    counts: dict[str, int] = {}
    for hit in probe_hits:
        counts[hit.memory.tier] = counts.get(hit.memory.tier, 0) + 1
    distribution = "、".join(
        f"{tier_label(tier)}{count}" for tier, count in sorted(counts.items(), reverse=True)
    )
    logging.getLogger(__name__).info(
        "memory.story 用户在确认你是否记得TA，注入%d条关于用户的记忆（等级：%s）",
        len(probe_hits),
        distribution,
    )
