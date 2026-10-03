"""Configured model adapter with owned meeting Run/source/budget checks."""

from collections.abc import Callable
from typing import Protocol

from app.config import ConfigStore, DatabaseConfigStore, HubConfig
from app.db import Database
from app.harness.budget import BudgetDenied
from app.llm import CompletionRequest, CompletionResult, LLMMessage, LLMRoute
from app.llm.factory import build_router
from app.llm.provider import EnvSecretProvider
from app.runs.completion import complete_owned_with_run
from app.schemas.common import PrivacyLevel

from .summary_ports import MeetingSummaryCompletion, MeetingSummaryInput


class CompletionBackend(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResult: ...


class SqlMeetingSummaryCompletion:
    def __init__(
        self,
        config_store: ConfigStore | DatabaseConfigStore,
        *,
        router_builder: Callable[[HubConfig], CompletionBackend] | None = None,
        database: Database | None = None,
    ) -> None:
        self._config_store = config_store
        self._database = database
        secrets = EnvSecretProvider()
        self._router_builder = router_builder or (lambda config: build_router(config, secrets))

    async def complete(self, request: MeetingSummaryInput) -> MeetingSummaryCompletion:
        privacy = PrivacyLevel(request.privacy_level)
        if privacy is PrivacyLevel.L3:
            raise BudgetDenied("l3_model_forbidden")
        snapshot = (
            await self._config_store.refresh()
            if isinstance(self._config_store, DatabaseConfigStore)
            else self._config_store.current
        )
        backend = self._router_builder(snapshot.config)
        completion = CompletionRequest(
            trace_id=request.meeting_id,
            messages=[
                LLMMessage(role="system", content=request.instruction),
                LLMMessage(role="user", content=request.text),
            ],
            privacy_level=privacy,
            route=LLMRoute.PRIVATE if privacy is PrivacyLevel.L2 else LLMRoute.UTILITY,
            temperature=0.0,
            json_mode=True,
            max_tokens=4_096,
        )
        result = (
            await complete_owned_with_run(
                self._database, snapshot, completion, backend.complete, kind="meeting.summary"
            )
            if self._database is not None
            else await backend.complete(completion)
        )
        return MeetingSummaryCompletion(result.text)
