"""Pure structured policy preserves safe outcomes and accepts detached completion DTOs."""

from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest
from test_cognition import semantic_event

from app.cognition.models import AttentionResult, CognitiveDecision, SemanticEvent, WorldState
from app.cognition.ports import DeliberationCompletionResult
from app.cognition.rule import RuleBasedDeliberator
from app.cognition.structured import TERMINAL_COMPLETION_REJECTIONS, StructuredDeliberator
from app.harness.budget import BudgetDenied
from app.ids import uuid7
from app.schemas import PrivacyLevel
from scripts import check_architecture


def inputs() -> tuple[SemanticEvent, WorldState, AttentionResult]:
    return (
        semantic_event(uuid7(), "user_arrived_home"),
        WorldState(built_at=datetime.now(UTC), timezone="UTC"),
        AttentionResult(score=0.8, threshold=0.5, reason_codes=["fixture"], should_deliberate=True),
    )


class Completion:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0
        self.error: Exception | None = None

    async def complete(
        self, event: SemanticEvent, state: WorldState, attention: AttentionResult
    ) -> DeliberationCompletionResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return DeliberationCompletionResult(self.text, "fixture.provider", "fixture-model")


class Fallback:
    def __init__(self) -> None:
        self.calls = 0

    async def deliberate(
        self, event: SemanticEvent, state: WorldState, attention: AttentionResult
    ) -> CognitiveDecision:
        self.calls += 1
        return await RuleBasedDeliberator().deliberate(event, state, attention)


@pytest.mark.parametrize("kind", ["ignore", "record", "inform", "ask", "suggest", "escalate"])
async def test_completion_port_preserves_valid_structured_policy(kind: str) -> None:
    completion = Completion(
        json.dumps(
            {
                "decision": kind,
                "message": "Fixture",
                "reason_codes": ["relevant"],
                "confidence": 0.7,
            }
        )
    )
    event, state, attention = inputs()
    decision = await StructuredDeliberator(completion).deliberate(event, state, attention)
    assert decision.decision == kind
    assert decision.event_id == event.event_id and decision.user_id == event.user_id
    assert decision.evidence_ids == event.evidence_ids
    assert decision.created_at == state.built_at
    assert decision.model_provider == "fixture.provider" and decision.model_name == "fixture-model"
    assert decision.approval_required == (kind in {"ask", "suggest"})
    assert completion.calls == 1


@pytest.mark.parametrize(
    "payload",
    [
        "not JSON",
        "[]",
        "{}",
        '{"decision":"act"}',
        '{"decision":"unknown"}',
        '{"decision":"inform"}',
        '{"decision":"inform","message":"Fixture","confidence":2}',
        '{"decision":"inform","message":"Fixture","urgency":"unsupported"}',
    ],
)
async def test_bad_completion_uses_existing_rule_fallback(payload: str) -> None:
    completion, fallback = Completion(payload), Fallback()
    decision = await StructuredDeliberator(completion, fallback=fallback).deliberate(*inputs())
    assert decision.decision == "suggest" and decision.model_provider is None
    assert completion.calls == 1 and fallback.calls == 1


@pytest.mark.parametrize("reason", sorted(TERMINAL_COMPLETION_REJECTIONS))
async def test_terminal_admission_rejection_never_falls_back(reason: str) -> None:
    completion, fallback = Completion("{}"), Fallback()
    completion.error = BudgetDenied(reason)
    with pytest.raises(BudgetDenied, match=reason):
        await StructuredDeliberator(completion, fallback=fallback).deliberate(*inputs())
    assert fallback.calls == 0


async def test_nonterminal_budget_rejection_preserves_rule_fallback() -> None:
    completion, fallback = Completion("{}"), Fallback()
    completion.error = BudgetDenied("budget_model_calls_exceeded")
    decision = await StructuredDeliberator(completion, fallback=fallback).deliberate(*inputs())
    assert decision.decision == "suggest" and fallback.calls == 1


@pytest.mark.parametrize("mode", ["passive", "private"])
async def test_ephemeral_and_passive_policy_never_calls_completion_port(mode: str) -> None:
    completion = Completion("{}")
    event, state, attention = inputs()
    event = event.model_copy(
        update={"passive": True} if mode == "passive" else {"privacy_level": PrivacyLevel.L3}
    )
    await StructuredDeliberator(completion).deliberate(event, state, attention)
    assert completion.calls == 0


@pytest.mark.parametrize("fallback", [False, True])
async def test_input_changes_during_completion_do_not_change_decision_sources(
    fallback: bool,
) -> None:
    event, state, attention = inputs()
    original_evidence = list(event.evidence_ids)
    entered, release = asyncio.Event(), asyncio.Event()

    class MutatingCompletion:
        async def complete(
            self, value: SemanticEvent, world: WorldState, score: AttentionResult
        ) -> DeliberationCompletionResult:
            assert value.evidence_ids == original_evidence
            entered.set()
            await release.wait()
            value.evidence_ids.append("adapter-changed")
            score.reason_codes.append("adapter-changed")
            world.memory_evidence_ids.append("adapter-changed")
            return DeliberationCompletionResult(
                "{}" if fallback else '{"decision":"inform","message":"Fixture"}',
                "fixture.provider",
                "fixture-model",
            )

    task = asyncio.create_task(
        StructuredDeliberator(MutatingCompletion()).deliberate(event, state, attention)
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        event.evidence_ids.append("caller-changed")
        attention.reason_codes.append("caller-changed")
        state.memory_evidence_ids.append("caller-changed")
        release.set()
        decision = await task
        assert decision.evidence_ids == original_evidence
        assert "caller-changed" not in decision.reason_codes
        assert "adapter-changed" not in decision.reason_codes
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelling_completion_does_not_start_rule_fallback() -> None:
    entered = asyncio.Event()
    fallback = Fallback()

    class Waiting:
        async def complete(
            self, event: SemanticEvent, state: WorldState, attention: AttentionResult
        ) -> DeliberationCompletionResult:
            entered.set()
            await asyncio.Future[None]()
            raise AssertionError("cancelled port must not return")

    task = asyncio.create_task(
        StructuredDeliberator(Waiting(), fallback=fallback).deliberate(*inputs())
    )
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fallback.calls == 0


def test_completion_result_is_frozen_and_legacy_rule_exports_keep_identity() -> None:
    from app.cognition import RuleBasedDeliberator as public
    from app.cognition.deliberation import RuleBasedDeliberator as legacy

    assert public is legacy is RuleBasedDeliberator
    result = DeliberationCompletionResult("{}", "fixture", "fixture")
    for name in result.__dataclass_fields__:
        with pytest.raises(FrozenInstanceError):
            setattr(result, name, "changed")


@pytest.mark.parametrize("module", ["rule", "structured", "action", "reflection"])
def test_decision_cores_reject_provider_and_database_dependencies(module: str) -> None:
    source = f"app.cognition.{module}"
    assert source in check_architecture.PURE_COGNITIVE_FLOW
    for target in (
        "app.db",
        "app.config",
        "app.llm.factory",
        "app.cognition.store",
        "app.cognition.deliberation_sql",
        "sqlalchemy.sql",
        "sqlite3",
        "asyncpg",
        "httpx.AsyncClient",
        "openai",
    ):
        assert not check_architecture.allowed(source, target)
