"""Focus evidence is bounded and belongs to the requested analysis window."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from app.focus.core import FocusSessionService
from app.timeline.models import TimelineEvent, TimelineSearchResult

NOW = datetime(2026, 10, 3, tzinfo=UTC)


class Repository:
    def __init__(self) -> None:
        self.owner = uuid4()
        self.event = TimelineEvent(
            id=1,
            user_id=self.owner,
            occurred_at=NOW - timedelta(minutes=5),
            ended_at=None,
            source_type="device",
            source_id=str(uuid4()),
            actor="device",
            event_type="screen.observed",
            conversation_id=None,
            title=None,
            summary="Synthetic unrelated topic",
            privacy_level="L1",
            importance=0.2,
            entities=(),
            keywords=(),
            metadata={},
            created_at=NOW,
        )
        self.calls: list[dict[str, Any]] = []

    async def search(self, **kwargs: Any) -> TimelineSearchResult:
        self.calls.append(kwargs)
        return TimelineSearchResult((self.event,), 1)


@pytest.mark.parametrize(
    "change",
    [
        {"user_id": uuid4()},
        {"privacy_level": "L2"},
        {"privacy_level": "L3"},
        {"privacy_level": "invalid"},
        {"event_type": "browser.observed"},
        {"occurred_at": NOW + timedelta(seconds=1)},
        {"occurred_at": NOW - timedelta(minutes=481)},
        {"summary": "  "},
    ],
)
async def test_rejects_unusable_observations_before_analysis(change: dict[str, Any]) -> None:
    repository = Repository()
    repository.event = replace(repository.event, **change)
    service = FocusSessionService(repository, clock=lambda: NOW)
    session = service.start_session(
        str(repository.owner),
        target="Synthetic target",
        keywords=["owned target"],
        duration_minutes=60,
    )
    evaluation = await service.evaluate_snapshot(session)
    assert evaluation.signals == ()
    assert evaluation.references == ()


@pytest.mark.parametrize("privacy", ["L0", "L1"])
async def test_normal_owned_evidence_and_bounded_query(privacy: str) -> None:
    repository = Repository()
    repository.event = replace(repository.event, privacy_level=privacy)
    service = FocusSessionService(repository, clock=lambda: NOW)
    session = service.start_session(
        str(repository.owner),
        target="Synthetic target",
        keywords=["owned target"],
        duration_minutes=60,
    )
    result = await service.evaluate_snapshot(session)
    assert [signal.kind for signal in result.signals] == ["off_target"]
    (reference,) = result.references
    assert (reference.source_id, reference.owner_id, reference.privacy_level) == (
        "1",
        str(repository.owner),
        privacy,
    )
    assert reference.parent_id == repository.event.source_id
    assert len(repository.calls) == 1
    assert repository.calls[0] == {
        "user_id": repository.owner,
        "start_at": NOW - timedelta(minutes=480),
        "end_at": NOW,
        "event_types": ("screen.observed",),
        "privacy_levels": ("L0", "L1"),
        "limit": 200,
        "candidate_limit": 201,
    }


async def test_sqlite_naive_observation_time_is_normalized() -> None:
    repository = Repository()
    repository.event = replace(repository.event, occurred_at=NOW.replace(tzinfo=None))
    service = FocusSessionService(repository, clock=lambda: NOW)
    session = service.start_session(
        str(repository.owner),
        target="Synthetic target",
        keywords=[],
        duration_minutes=60,
    )
    result = await service.evaluate_snapshot(session)
    assert result.references and result.signals
