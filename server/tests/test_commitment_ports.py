"""Commitment policy can run using detached repository and completion ports."""

from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.cognition.commitment_ports import CommitmentCompletion, CommitmentInput
from app.cognition.commitments import CommitmentTracker
from app.cognition.goal_tracker import GoalTracker, commitment_instruction
from app.cognition.models import GoalKind, GoalStatus, GoalView
from app.ids import uuid7
from app.llm.contracts import CompletionRequest, LLMMessage, LLMRoute
from app.schemas import PrivacyLevel
from scripts.check_architecture import allowed


class Repository:
    def __init__(self, *, reject: bool = False) -> None:
        self.goal: GoalView | None = None
        self.owners: list[UUID] = []
        self.reject = reject

    async def goal_by_source(
        self, user_id: UUID, *, source_kind: str, source_id: str
    ) -> GoalView | None:
        self.owners.append(user_id)
        return self.goal

    async def create_goal(
        self,
        *,
        user_id: UUID,
        kind: GoalKind,
        title: str,
        source_kind: str,
        source_id: str,
        due_at: datetime | None = None,
        privacy_level: PrivacyLevel | None = None,
    ) -> GoalView:
        if self.reject:
            raise ValueError("source revoked")
        self.owners.append(user_id)
        self.goal = GoalView(
            id=uuid7(),
            kind=kind,
            title=title,
            status=GoalStatus.ACTIVE,
            source_kind=source_kind,
            source_id=source_id,
            due_at=due_at,
            privacy_level=privacy_level,
        )
        return self.goal


class Completion:
    def __init__(self, text: str) -> None:
        self.text = text
        self.inputs: list[CommitmentInput] = []

    async def complete(self, request: CommitmentInput) -> CommitmentCompletion:
        self.inputs.append(request)
        return CommitmentCompletion(self.text)


VALID = '{"commitments":[{"title":"Synthetic goal","confidence":0.9,"due_at":null}]}'


@pytest.mark.parametrize("privacy", [PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2])
async def test_legacy_chat_adapter_preserves_completion_request(privacy: PrivacyLevel) -> None:
    class Backend:
        def __init__(self) -> None:
            self.requests: list[CompletionRequest] = []

        async def complete(self, request: CompletionRequest) -> object:
            self.requests.append(request)
            return SimpleNamespace(text=VALID)

    backend, repository = Backend(), Repository()
    message = uuid7()
    values = await GoalTracker(repository).ingest_message(
        user_id=uuid7(),
        message_id=message,
        text="x" * 4100,
        privacy_level=privacy,
        backend=backend,
        strict=True,
    )
    assert len(values) == 1
    assert len(backend.requests) == 1
    request = backend.requests[0]
    assert request.trace_id == message and request.privacy_level == privacy
    assert request.route == LLMRoute.UTILITY and request.temperature == 0
    assert request.json_mode is True and request.max_tokens == 1024
    assert request.messages[0].role == "system" and request.messages[1].role == "user"
    assert request.messages[1].content == "x" * 4000
    assert ("隐私约束" in request.messages[0].content) == (privacy == PrivacyLevel.L2)


@pytest.mark.parametrize("privacy", [PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2])
async def test_pure_commitment_policy_preserves_owned_message_and_privacy(
    privacy: PrivacyLevel,
) -> None:
    repository, completion = Repository(), Completion(VALID)
    owner, message = uuid7(), uuid7()
    tracker = CommitmentTracker(repository)
    values = await tracker.ingest_message(
        user_id=owner,
        message_id=message,
        text="Synthetic commitment",
        privacy_level=privacy,
        completion=completion,
    )
    assert len(values) == 1 and values[0].source_id == str(message)
    assert values[0].privacy_level == privacy
    assert repository.owners == [owner, owner]
    assert completion.inputs == [CommitmentInput(message, "Synthetic commitment", privacy)]
    assert (
        await tracker.ingest_message(
            user_id=owner,
            message_id=message,
            text="Repeated commitment",
            privacy_level=privacy,
            completion=completion,
        )
        == []
    )


@pytest.mark.parametrize("case", ["private", "empty", "missing"])
async def test_private_empty_or_unconfigured_commitment_does_no_work(case: str) -> None:
    repository, completion = Repository(), Completion(VALID)
    assert (
        await CommitmentTracker(repository).ingest_message(
            user_id=uuid7(),
            message_id=uuid7(),
            text=" " if case == "empty" else "Synthetic commitment",
            privacy_level=PrivacyLevel.L3 if case == "private" else PrivacyLevel.L1,
            completion=None if case == "missing" else completion,
        )
        == []
    )
    assert repository.owners == [] and completion.inputs == []


@pytest.mark.parametrize(
    "text",
    [
        "garbage",
        "[]",
        '{"commitments":[{"title":"x","confidence":1}]}',
        '{"commitments":[{"title":"Synthetic goal","confidence":0.6}]}',
        '{"commitments":[{"title":"Synthetic goal","confidence":2}]}',
        '{"commitments":[{"title":"Synthetic goal","confidence":1,"unexpected":true}]}',
        '{"commitments":[{"title":"Synthetic goal","confidence":1,"due_at":"bad"}]}',
        json.dumps({"commitments": [{"title": "Synthetic goal", "confidence": 1}] * 4}),
    ],
)
async def test_invalid_or_uncertain_completion_does_not_create_goals(text: str) -> None:
    repository = Repository()
    assert (
        await CommitmentTracker(repository).ingest_message(
            user_id=uuid7(),
            message_id=uuid7(),
            text="Synthetic commitment",
            privacy_level=PrivacyLevel.L1,
            completion=Completion(text),
            strict=True,
        )
        == []
    )
    assert repository.goal is None


async def test_fenced_json_truncation_and_repository_rejection() -> None:
    repository, completion = Repository(reject=True), Completion(f"```json\n{VALID}\n```")
    assert (
        await CommitmentTracker(repository).ingest_message(
            user_id=uuid7(),
            message_id=uuid7(),
            text="x" * 4100,
            privacy_level=PrivacyLevel.L2,
            completion=completion,
        )
        == []
    )
    assert len(completion.inputs[0].text) == 4000
    assert repository.goal is None


@pytest.mark.parametrize("strict", [False, True])
async def test_completion_error_preserves_strict_behavior(strict: bool) -> None:
    class Broken:
        async def complete(self, request: CommitmentInput) -> CommitmentCompletion:
            raise RuntimeError("fixture completion failed")

    operation = CommitmentTracker(Repository()).ingest_message(
        user_id=uuid7(),
        message_id=uuid7(),
        text="Synthetic commitment",
        privacy_level=PrivacyLevel.L1,
        completion=Broken(),
        strict=strict,
    )
    if strict:
        with pytest.raises(RuntimeError, match="fixture completion failed"):
            await operation
    else:
        assert await operation == []


async def test_completion_cancellation_does_not_start_repository_work() -> None:
    class Cancelled:
        async def complete(self, request: CommitmentInput) -> CommitmentCompletion:
            raise asyncio.CancelledError()

    repository = Repository()
    with pytest.raises(asyncio.CancelledError):
        await CommitmentTracker(repository).ingest_message(
            user_id=uuid7(),
            message_id=uuid7(),
            text="Synthetic commitment",
            privacy_level=PrivacyLevel.L1,
            completion=Cancelled(),
        )
    assert repository.owners == []


def test_commitment_port_values_are_frozen() -> None:
    def overwrite(value: object, attribute: str) -> None:
        setattr(value, attribute, "changed")

    request = CommitmentInput(uuid7(), "Synthetic commitment", PrivacyLevel.L1)
    with pytest.raises(FrozenInstanceError):
        overwrite(request, "text")
    with pytest.raises(FrozenInstanceError):
        overwrite(CommitmentCompletion(VALID), "text")


@pytest.mark.parametrize("module", ["app.cognition.commitments", "app.cognition.commitment_ports"])
@pytest.mark.parametrize("adapter", ["app.db", "app.llm", "app.memory.extraction", "httpx"])
def test_commitment_core_cannot_import_runtime_adapters(module: str, adapter: str) -> None:
    assert not allowed(module, adapter)


@pytest.mark.parametrize("privacy", [PrivacyLevel.L2, PrivacyLevel.L3])
async def test_commitment_policy_normalizes_privacy_from_real_request_dto(
    privacy: PrivacyLevel,
) -> None:
    request = CompletionRequest(
        trace_id=uuid7(),
        messages=[LLMMessage(role="user", content="Synthetic commitment")],
        privacy_level=privacy,
        route=LLMRoute.PRIVATE,
    )
    # StrictModel serializes enum values into strings even though its Python
    # attribute is annotated with the enum type. This is the real chat shape.
    assert type(request.privacy_level) is str
    repository, completion = Repository(), Completion(VALID)
    values = await CommitmentTracker(repository).ingest_message(
        user_id=uuid7(),
        message_id=request.trace_id,
        text=request.messages[0].content,
        privacy_level=request.privacy_level,
        completion=completion,
    )
    if privacy == PrivacyLevel.L3:
        assert values == [] and completion.inputs == [] and repository.owners == []
    else:
        assert len(values) == 1
        assert completion.inputs[0].privacy_level is PrivacyLevel.L2
        assert "隐私约束" in commitment_instruction(request.privacy_level)
