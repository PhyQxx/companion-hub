"""Runtime completion adapter owns configuration, providers and source-fenced Runs."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Protocol

from app.config import ConfigStore, DatabaseConfigStore, HubConfig
from app.db import Database
from app.llm import CompletionRequest, CompletionResult, LLMMessage, LLMRoute
from app.llm.factory import build_router
from app.llm.provider import EnvSecretProvider
from app.runs.completion import complete_with_run

from .models import AttentionResult, SemanticEvent, WorldState
from .ports import DeliberationCompletionResult
from .store import CognitiveStore


class CompletionBackend(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResult: ...


class SqlDeliberationCompletion:
    def __init__(
        self,
        config_store: ConfigStore | DatabaseConfigStore,
        *,
        database: Database | None = None,
        router_builder: Callable[[HubConfig], CompletionBackend] | None = None,
    ) -> None:
        self._config_store = config_store
        self._database = database
        secrets = EnvSecretProvider()
        self._router_builder = router_builder or (lambda config: build_router(config, secrets))

    async def complete(
        self, event: SemanticEvent, state: WorldState, attention: AttentionResult
    ) -> DeliberationCompletionResult:
        event = event.model_copy(deep=True)
        state = state.model_copy(deep=True)
        attention = attention.model_copy(deep=True)
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
                async with self._database.sessions.begin() as session:
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
        return DeliberationCompletionResult(result.text, result.provider, result.model)
