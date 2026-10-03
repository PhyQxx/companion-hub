"""Message extraction handles the actual string privacy in request DTOs."""

from datetime import UTC, datetime

import pytest

from app.ids import uuid7
from app.llm.contracts import CompletionRequest, CompletionResult, LLMMessage, LLMRoute
from app.memory.extraction import LlmMemoryExtractor, extraction_instruction
from app.schemas.common import PrivacyLevel


@pytest.mark.parametrize("privacy", [PrivacyLevel.L2, PrivacyLevel.L3])
async def test_memory_extractor_normalizes_privacy_from_real_request(privacy: PrivacyLevel) -> None:
    requests: list[CompletionRequest] = []

    class Backend:
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            requests.append(request)
            return CompletionResult(
                text='{"candidates":[]}',
                provider="fixture",
                model="fixture",
                endpoint="fixture",
                route=request.route,
                latency_ms=0,
            )

    request = CompletionRequest(
        trace_id=uuid7(),
        messages=[LLMMessage(role="user", content="Synthetic")],
        privacy_level=privacy,
        route=LLMRoute.PRIVATE,
    )
    assert type(request.privacy_level) is str
    result = await LlmMemoryExtractor().extract(
        "Synthetic",
        message_id=request.trace_id,
        privacy_level=request.privacy_level,
        occurred_at=datetime.now(UTC),
        backend=Backend(),
    )
    assert result == []
    if privacy is PrivacyLevel.L3:
        assert requests == []
    else:
        assert len(requests) == 1 and "隐私约束" in requests[0].messages[0].content
        assert "隐私约束" in extraction_instruction(request.privacy_level)
