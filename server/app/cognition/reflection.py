# ruff: noqa: RUF001
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from .models import ReflectionCandidate


class ReflectionStore(Protocol):
    async def distinct_trigger_kinds(
        self, user_id: UUID, *, since: datetime
    ) -> list[str]: ...

    async def feedback_summary(
        self,
        *,
        user_id: UUID,
        trigger_kind: str,
        since: datetime,
    ) -> FeedbackSummary: ...

    async def save_candidate(
        self,
        *,
        user_id: UUID,
        candidate: ReflectionCandidate,
    ) -> UUID: ...


@dataclass(frozen=True, slots=True)
class FeedbackSummary:
    total: int
    accepted: int
    ignored: int
    snoozed: int
    forbidden: int
    acceptance_rate: float


class ReflectionEngine:
    """Analyse feedback patterns and generate preference candidates.

    V1 uses deterministic aggregation (counts and rates).  Model-assisted
    deep pattern recognition is reserved for v2.
    """

    def __init__(
        self,
        store: ReflectionStore,
        *,
        ignored_threshold: int = 3,
        forbidden_threshold: int = 2,
        min_total: int = 3,
        lookback_days: float = 7.0,
    ) -> None:
        self._store = store
        self._ignored_threshold = ignored_threshold
        self._forbidden_threshold = forbidden_threshold
        self._min_total = min_total
        self._lookback = timedelta(days=lookback_days)

    async def run_for_user(
        self, user_id: UUID, *, now: datetime | None = None
    ) -> list[ReflectionCandidate]:
        """Run a periodic reflection pass for a single user.

        Returns newly generated candidates that require confirmation.
        """
        moment = now or datetime.now(UTC)
        since = moment - self._lookback

        # Gather all trigger kinds that have feedback in the lookback window.
        trigger_kinds = await self._store.distinct_trigger_kinds(user_id, since=since)

        candidates: list[ReflectionCandidate] = []
        for kind in trigger_kinds:
            summary = await self._store.feedback_summary(
                user_id=user_id, trigger_kind=kind, since=since
            )
            candidate = self._analyse(summary, trigger_kind=kind)
            if candidate is not None:
                await self._store.save_candidate(
                    user_id=user_id,
                    candidate=candidate,
                )
                candidates.append(candidate)

        return candidates

    def _analyse(
        self, summary: FeedbackSummary, *, trigger_kind: str
    ) -> ReflectionCandidate | None:
        if summary.total < self._min_total:
            return None

        # Forbidden is the strongest signal.
        if summary.forbidden >= self._forbidden_threshold:
            return ReflectionCandidate(
                content=f"用户明确禁止了 {trigger_kind} 类主动提醒，建议永久关闭该类型。",
                evidence_ids=[],
                confidence=min(0.5 + 0.15 * summary.forbidden, 0.95),
                requires_confirmation=True,
            )

        # High ignore rate suggests frequency or relevance problems.
        ignore_rate = summary.ignored / summary.total
        if ignore_rate >= 0.6 and summary.ignored >= self._ignored_threshold:
            return ReflectionCandidate(
                content=(
                    f"用户对 {trigger_kind} 类提醒的忽略率较高"
                    f"({ignore_rate:.0%})，"
                    f"建议降低频率或仅在更紧急时触发。"
                ),
                evidence_ids=[],
                confidence=min(0.5 + 0.2 * ignore_rate, 0.9),
                requires_confirmation=True,
            )

        # Very high acceptance rate suggests a stable preference.
        if summary.acceptance_rate >= 0.8 and summary.total >= 5:
            return ReflectionCandidate(
                content=(
                    f"用户高度接受 {trigger_kind} 类提醒"
                    f"(接受率 {summary.acceptance_rate:.0%})，"
                    f"该类提醒的优先级可适当提升。"
                ),
                evidence_ids=[],
                confidence=min(0.6 + 0.1 * summary.acceptance_rate, 0.9),
                requires_confirmation=False,
            )

        return None
