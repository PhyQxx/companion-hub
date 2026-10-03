"""Detached focus services preserve interruption and public facade contracts."""

import asyncio
from typing import Any

import pytest
from test_focus_query_boundary import NOW, Repository

from app.focus import FocusEvaluation, FocusSessionService
from app.focus.core import FocusEvaluation as CoreEvaluation
from app.focus.service import FocusEvaluation as LegacyEvaluation
from app.timeline.models import TimelineSearchResult
from scripts.check_architecture import allowed


@pytest.mark.parametrize("cancel", [False, True])
async def test_query_failure_or_cancel_does_not_retry_or_change_session(cancel: bool) -> None:
    class Broken(Repository):
        async def search(self, **kwargs: Any) -> TimelineSearchResult:
            self.calls.append(kwargs)
            if cancel:
                raise asyncio.CancelledError
            raise RuntimeError("synthetic query failure")

    repository = Broken()
    service = FocusSessionService(repository, clock=lambda: NOW)
    session = service.start_session(
        str(repository.owner),
        target="Synthetic target",
        keywords=[],
        duration_minutes=60,
    )
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await service.evaluate_snapshot(session)
    assert len(repository.calls) == 1
    assert service.get_session(str(repository.owner)) is session
    assert not session.nudged_at


def test_facade_evaluation_alias_and_unchanged_session_cooldown() -> None:
    assert FocusEvaluation is CoreEvaluation is LegacyEvaluation
    repository = Repository()
    service = FocusSessionService(repository, clock=lambda: NOW)
    keywords = [" fixture "]
    session = service.start_session(
        str(repository.owner),
        target="Synthetic target",
        keywords=keywords,
        duration_minutes=60,
    )
    keywords.clear()
    assert session.target_keywords == ("fixture",)
    assert service.active_sessions() == [session]
    assert service.stop_session(str(repository.owner)) is session


@pytest.mark.parametrize("module", ["app.focus.core", "app.focus.analysis"])
@pytest.mark.parametrize(
    "dependency", ["app.db", "app.timeline.store", "sqlalchemy", "httpx", "openai"]
)
def test_focus_core_static_boundaries(module: str, dependency: str) -> None:
    assert not allowed(module, dependency)
