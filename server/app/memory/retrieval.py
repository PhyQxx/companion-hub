# ruff: noqa: RUF001, RUF002, RUF003
"""混合检索器：双路召回 → 硬过滤 → 归一化重排 → 类型配额 Top-K。

策略版本 hybrid-quota-v1 会随 decision_meta 一起落库，任何打分或
过滤规则的修改都必须 bump 版本号，保证历史回合可追溯当时的检索行为。
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import final
from uuid import UUID

from app.schemas.common import PrivacyLevel

from .embeddings import cosine_similarity, lexical_cosine, text_tokens
from .models import MemoryEntry, MemoryStatus, MemorySubjectKind, MemoryType
from .store import MemoryStore, RetrievalCandidate

RETRIEVAL_POLICY_VERSION = "hybrid-subject-v3"

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


@dataclass(frozen=True, slots=True)
class MemoryHit:
    memory: MemoryEntry
    vector_score: float
    lexical_score: float
    final_score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    hits: tuple[MemoryHit, ...]
    policy_version: str
    candidate_count: int
    vector_recalled: int
    lexical_recalled: int
    subject_hint: str | None = None
    fact_hint: str | None = None


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


@final
class MemoryRetriever:
    def __init__(self, store: MemoryStore, *, policy: RetrievalPolicy | None = None) -> None:
        self._store = store
        self._policy = policy or RetrievalPolicy()

    async def retrieve(
        self,
        query: str,
        *,
        user_id: UUID,
        privacy_level: PrivacyLevel,
        now: datetime | None = None,
    ) -> RetrievalResult:
        moment = now or datetime.now(UTC)
        privacy = PrivacyLevel(privacy_level)
        subject_hint = _infer_subject_hint(query)
        fact_hints = _infer_fact_keys(query)
        fact_hint = fact_hints[0] if fact_hints else None
        # 隐私闸门：L2 记忆只能进入强制本地路由的 L2 上下文，
        # 绝不允许随 L0/L1 云端调用出站
        allowed_levels = (
            (PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2)
            if privacy is PrivacyLevel.L2
            else (PrivacyLevel.L0, PrivacyLevel.L1)
        )
        candidates = await self._store.retrieval_candidates(
            user_id,
            subject_scopes=DEFAULT_SUBJECT_SCOPES,
            statuses=(MemoryStatus.ACTIVE,),
            privacy_levels=allowed_levels,
            valid_at=moment,
            limit=self._policy.candidate_limit,
        )
        provider = self._store.embedding_provider
        query_vector = (await provider.embed([query]))[0]
        query_tokens = text_tokens(query)

        def similarity_of(item: RetrievalCandidate) -> float:
            if (
                item.embedding is None
                or item.entry.embedding_version != provider.version
                or len(item.embedding) != provider.dimension
            ):
                return 0.0
            return cosine_similarity(query_vector, item.embedding)

        if self._store.vector_sql_enabled:
            # PostgreSQL 上用 pgvector ANN 替换全量扫描；
            # 两条路径产出同构的 (id, 相似度) 列表，进入同一套合并/重排
            vector_ranked = await self._store.vector_recall(
                query_vector,
                user_id=user_id,
                embedding_version=provider.version,
                privacy_levels=[level.value for level in allowed_levels],
                subject_keys=[key for _, key in DEFAULT_SUBJECT_SCOPES],
                valid_at=moment,
                limit=self._policy.recall_k,
            )
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
            reasons: list[str] = []
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
            exact_entries.extend(
                await self._store.fact_candidates(
                    user_id=user_id,
                    fact_key=exact_fact_key,
                    subject_scopes=DEFAULT_SUBJECT_SCOPES,
                    privacy_levels=allowed_levels,
                    valid_at=moment,
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
        )

    def grounded_hits(self, result: RetrievalResult) -> tuple[MemoryHit, ...]:
        """Return only hits strong enough to be treated as remembered evidence.

        Retrieval top-k is intentionally permissive so the model can receive useful
        context. Evidence semantics are stricter: exact fact slots always qualify,
        lexical matches need a minimum overlap, and vector-only matches need a high
        similarity. Importance, pin and subject bonuses may order relevant memories,
        but must never manufacture relevance by themselves.
        """
        return tuple(
            hit
            for hit in result.hits
            if "exact_fact" in hit.reasons
            or hit.lexical_score >= self._policy.grounded_lexical_threshold
            or hit.vector_score >= self._policy.grounded_vector_threshold
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
            return ""
        labels = {
            MemorySubjectKind.USER.value: "关于用户",
            MemorySubjectKind.ASSISTANT.value: "关于你自己",
            MemorySubjectKind.SHARED.value: "关于你们",
        }
        grouped: dict[str, list[str]] = {key: [] for key in labels}
        for hit in selected_hits:
            fact = f"[{hit.memory.fact_key}]" if hit.memory.fact_key else ""
            line = f"- [{hit.memory.type}]{fact} {hit.memory.content}"
            if hit.memory.summary:
                line += f"（{hit.memory.summary}）"
            grouped.setdefault(hit.memory.subject_kind, []).append(line)
        blocks = [
            "【长期记忆】以下内容来自可追溯的长期记忆。优先遵守 active 的稳定事实；"
            "不要把不同主体混淆。不确定时可以明确说不确定，不要编造。"
        ]
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


def _infer_subject_hint(query: str) -> MemorySubjectKind | None:
    normalized = query.strip().lower()
    if any(token in normalized for token in ("我们", "咱们", "咱俩", "我们俩")):
        return MemorySubjectKind.SHARED
    if any(token in normalized for token in ("你的", "你自己", "你叫什么", "你多高", "你多重")):
        return MemorySubjectKind.ASSISTANT
    if any(token in normalized for token in ("我的", "我自己", "我叫", "我是不是", "我喜欢")):
        return MemorySubjectKind.USER
    return None


def _infer_fact_keys(query: str) -> tuple[str, ...]:
    normalized = query.strip().lower()
    patterns: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("profile.measurements", ("三围", "身材数据", "身体围度", "胸围腰围臀围")),
        ("profile.height", ("身高", "多高")),
        ("profile.weight", ("体重", "多重")),
        ("profile.birthday", ("生日", "出生日期")),
        ("profile.nickname", ("昵称", "小名")),
        ("profile.name", ("名字", "姓名", "叫什么")),
        ("preference.food", ("饮食偏好", "喜欢吃", "爱吃", "不吃")),
        ("preference.drink", ("饮料偏好", "喜欢喝", "爱喝")),
    )
    return tuple(
        fact_key
        for fact_key, tokens in patterns
        if any(token in normalized for token in tokens)
    )


def _rank(
    candidates: Sequence[RetrievalCandidate],
    key: Callable[[RetrievalCandidate], float],
) -> list[tuple[int, float]]:
    ranked = [(item.entry.id, key(item)) for item in candidates]
    ranked = [(memory_id, score) for memory_id, score in ranked if score > 0]
    ranked.sort(key=lambda pair: pair[1], reverse=True)
    return ranked
