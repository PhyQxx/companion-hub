from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import NOW, UUIDS
from pydantic import ValidationError

from app.schemas import EphemeralSignal, InputEnvelope
from app.schemas.common import SourceRef


def test_input_envelope_accepts_uuid7_and_multimodal_content(source: SourceRef) -> None:
    event = InputEnvelope(
        event_id=UUIDS["event"],
        correlation_id=UUIDS["correlation"],
        user_id=UUIDS["user"],
        conversation_id=UUIDS["conversation"],
        turn_id=UUIDS["turn"],
        source=source,
        kind="message.received",
        occurred_at=NOW,
        received_at=NOW,
        privacy_level="L1",
        content=[
            {"type": "text", "text": "看看这张图"},
            {
                "type": "image",
                "asset_ref": {
                    "asset_id": UUIDS["intent"],
                    "media_type": "image/png",
                    "content_hash": f"sha256:{'a' * 64}",
                },
                "width": 1024,
                "height": 1024,
            },
        ],
    )
    assert [part.type for part in event.content] == ["text", "image"]


def test_input_envelope_rejects_uuid4(input_event: InputEnvelope) -> None:
    payload = input_event.model_dump(mode="json")
    payload["event_id"] = "550e8400-e29b-41d4-a716-446655440000"
    with pytest.raises(ValidationError, match="UUID version 7 expected"):
        InputEnvelope.model_validate(payload)


def test_l3_cannot_enter_durable_envelope(input_event: InputEnvelope) -> None:
    payload = input_event.model_dump(mode="json")
    payload["privacy_level"] = "L3"
    with pytest.raises(ValidationError, match="EphemeralSignal"):
        InputEnvelope.model_validate(payload)


def test_l3_ephemeral_signal_is_valid(ephemeral_signal: EphemeralSignal) -> None:
    assert ephemeral_signal.privacy_level == "L3"
    assert ephemeral_signal.content.type == "telemetry"


def test_ephemeral_expiry_must_follow_occurrence(
    ephemeral_signal: EphemeralSignal,
) -> None:
    payload = ephemeral_signal.model_dump()
    payload["expires_at"] = NOW - timedelta(seconds=1)
    with pytest.raises(ValidationError, match="expires_at"):
        EphemeralSignal.model_validate(payload)


def test_extensions_must_be_namespaced(input_event: InputEnvelope) -> None:
    payload = input_event.model_dump(mode="json")
    payload["extensions"] = {"camera": {"model": "test"}}
    with pytest.raises(ValidationError, match="namespaced"):
        InputEnvelope.model_validate(payload)
