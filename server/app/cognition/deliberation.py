# ruff: noqa: RUF001
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Protocol

from app.config import ConfigStore, DatabaseConfigStore, HubConfig
from app.db import Database
from app.harness.budget import BudgetDenied
from app.ids import uuid7
from app.llm import CompletionRequest, CompletionResult, LLMMessage, LLMRoute
from app.llm.factory import build_router
from app.llm.provider import EnvSecretProvider
from app.runs.completion import complete_with_run
from app.schemas import PrivacyLevel

from .models import (
    AttentionResult,
    CognitiveDecision,
    DecisionKind,
    SemanticEvent,
    Urgency,
    WorldState,
)
from .store import CognitiveStore

COGNITIVE_POLICY_VERSION = "cognitive-v1"


class Deliberator(Protocol):
    async def deliberate(
        self, event: SemanticEvent, state: WorldState, attention: AttentionResult
    ) -> CognitiveDecision: ...


class CompletionBackend(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResult: ...


class RuleBasedDeliberator:
    async def deliberate(
        self, event: SemanticEvent, state: WorldState, attention: AttentionResult
    ) -> CognitiveDecision:
        decision = DecisionKind.RECORD
        urgency = Urgency.LOW
        message: str | None = None
        reasons = [*attention.reason_codes]
        if event.passive:
            reasons.append("passive_response_required")
        elif event.kind in {"water_leak", "water_leak_detected", "safety.alarm"}:
            decision = DecisionKind.ESCALATE
            urgency = Urgency.CRITICAL
            raw_message = event.attributes.get("message")
            message = raw_message if isinstance(raw_message, str) else None
            message = message or "检测到水浸或安全告警，请立即检查现场。"
            reasons.append("critical_safety_event")
        elif event.kind == "user_arrived_home":
            decision = DecisionKind.SUGGEST
            urgency = Urgency.NORMAL
            message = "欢迎回家。我已经看到家里的状态，需要我帮你检查灯光或温度吗？"
            reasons.append("presence_confirmed")
        elif event.kind == "light_on_too_long":
            decision = DecisionKind.ASK
            urgency = Urgency.NORMAL
            message = "有一盏灯已经开了较长时间，需要我帮你关闭吗？"
            reasons.append("sustained_state")
        elif event.kind == "temperature_high":
            decision = DecisionKind.SUGGEST
            urgency = Urgency.NORMAL
            message = "室内温度持续偏高，需要我帮你调整空调吗？"
            reasons.append("sustained_state")
        else:
            decision = DecisionKind.INFORM
            urgency = Urgency.NORMAL
            message = event.summary
        return CognitiveDecision(
            id=uuid7(),
            event_id=event.event_id,
            user_id=event.user_id,
            conversation_id=event.conversation_id,
            trigger_kind=event.kind,
            decision=decision,
            reason_codes=list(dict.fromkeys(reasons)),
            evidence_ids=event.evidence_ids,
            confidence=event.confidence,
            urgency=urgency,
            attention_score=attention.score,
            policy_version=COGNITIVE_POLICY_VERSION,
            message=message,
            approval_required=decision in {DecisionKind.ASK, DecisionKind.SUGGEST},
            expires_at=event.expires_at,
            created_at=state.built_at,
        )


class RouterDeliberator:
    """Model-backed structured judgment with a deterministic safe fallback."""

    def __init__(
        self,
        config_store: ConfigStore | DatabaseConfigStore,
        *,
        router_builder: Callable[[HubConfig], CompletionBackend] | None = None,
        fallback: Deliberator | None = None,
        database: Database | None = None,
    ) -> None:
        self._config_store = config_store
        self._database = database
        secrets = EnvSecretProvider()
        self._router_builder = router_builder or (lambda config: build_router(config, secrets))
        self._fallback = fallback or RuleBasedDeliberator()

    async def deliberate(
        self, event: SemanticEvent, state: WorldState, attention: AttentionResult
    ) -> CognitiveDecision:
        if event.passive or event.privacy_level == PrivacyLevel.L3:
            return await self._fallback.deliberate(event, state, attention)
        try:
            snapshot = (
                await self._config_store.refresh()
                if isinstance(self._config_store, DatabaseConfigStore)
                else self._config_store.current
            )
            backend = self._router_builder(snapshot.config)
            request = CompletionRequest(
                trace_id=event.event_id,
                messages=[
                    LLMMessage(
                        role="system",
                        content=(
                            "你是 Aria 的认知调度器。只根据给定证据决定是否打扰用户。"
                            "输出 JSON, decision 只能是 "
                            "ignore/record/inform/ask/suggest/escalate。"
                            "reason_codes 是简短枚举数组, confidence 0..1, "
                            "urgency 是 low/normal/high/critical。"
                            "message 是可选简短中文。不得输出 act，不得虚构事实。"
                        ),
                    ),
                    LLMMessage(
                        role="user",
                        content=json.dumps(
                            {
                                "event": {
                                    "kind": event.kind,
                                    "summary": event.summary,
                                    "confidence": event.confidence,
                                    "evidence_ids": event.evidence_ids,
                                },
                                "world": state.model_dump(mode="json"),
                                "attention": attention.model_dump(mode="json"),
                            },
                            ensure_ascii=False,
                        ),
                    ),
                ],
                privacy_level=event.privacy_level,
                route=LLMRoute.DIALOGUE,
                max_tokens=500,
                temperature=0,
                json_mode=True,
            )

            async def validate_sources() -> None:
                if self._database is not None:
                    async with self._database.sessions() as session:
                        await CognitiveStore(self._database).validate_world_snapshot(
                            session, event, state
                        )

            result = (
                await complete_with_run(
                    self._database,
                    snapshot,
                    request,
                    backend.complete,
                    user_id=event.user_id,
                    kind="cognition.deliberation",
                    source_id=event.event_id,
                    conversation_id=event.conversation_id,
                    expires_at=event.expires_at,
                    source_guard=validate_sources,
                )
                if self._database is not None
                else await backend.complete(request)
            )
            payload = json.loads(result.text)
            decision = DecisionKind(payload["decision"])
            if decision == DecisionKind.ACT:
                raise ValueError("act is disabled")
            message = str(payload.get("message") or "").strip() or None
            return CognitiveDecision(
                id=uuid7(),
                event_id=event.event_id,
                user_id=event.user_id,
                conversation_id=event.conversation_id,
                trigger_kind=event.kind,
                decision=decision,
                reason_codes=[str(item)[:80] for item in payload.get("reason_codes", [])][:8],
                evidence_ids=event.evidence_ids,
                confidence=float(payload.get("confidence", event.confidence)),
                urgency=Urgency(payload.get("urgency", "normal")),
                attention_score=attention.score,
                policy_version=COGNITIVE_POLICY_VERSION,
                message=message,
                approval_required=decision in {DecisionKind.ASK, DecisionKind.SUGGEST},
                model_provider=result.provider,
                model_name=result.model,
                expires_at=event.expires_at,
                created_at=state.built_at,
            )
        except Exception as error:
            if isinstance(error, BudgetDenied) and error.reason_code in {
                "budget_run_inactive",
                "run_source_not_found",
                "model_run_owner_missing",
                "budget_owner_invalid",
                "model_source_changed",
                "model_source_check_failed",
                "model_run_source_already_processed",
                "run_deadline_exceeded",
            }:
                raise
            return await self._fallback.deliberate(event, state, attention)
