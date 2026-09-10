from datetime import UTC, datetime, timedelta

import pytest

from app.confirmation import PendingMutationStore
from app.ids import uuid7


def test_confirmation_preview_is_bound_replaced_and_expires() -> None:
    store = PendingMutationStore(limit=3)
    owner, outsider, turn = uuid7(), uuid7(), uuid7()
    first = store.prepare(
        user_id=owner,
        turn_id=turn,
        kind="calendar_create",
        content={"title": "A"},
        preview={"title": "A"},
    )
    replay = store.prepare(
        user_id=owner,
        turn_id=turn,
        kind="calendar_create",
        content={"title": "A"},
        preview={"title": "A"},
    )
    replacement = store.prepare(
        user_id=owner,
        turn_id=turn,
        kind="calendar_create",
        content={"title": "B"},
        preview={"title": "B"},
    )
    assert replay.id == first.id
    assert first.status == "cancelled"
    assert replacement.id != first.id
    with pytest.raises(LookupError):
        store.claim(outsider, replacement.id, replacement.digest)
    with pytest.raises(ValueError, match="changed"):
        store.claim(owner, replacement.id, "0" * 64)

    expired = store.prepare(
        user_id=owner,
        turn_id=uuid7(),
        kind="workflow_save",
        content={"name": "old"},
        preview={"name": "old"},
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    with pytest.raises(ValueError, match="expired"):
        store.claim(owner, expired.id, expired.digest)


def test_confirmation_claim_is_single_use_and_completion_replays() -> None:
    store = PendingMutationStore()
    owner = uuid7()
    item = store.prepare(
        user_id=owner,
        turn_id=uuid7(),
        kind="workflow_save",
        content={"name": "A"},
        preview={"name": "A"},
    )
    claimed = store.claim(owner, item.id, item.digest)
    with pytest.raises(ValueError, match="not_pending"):
        store.claim(owner, item.id, item.digest)
    completed = store.complete(claimed, {"id": "saved"})
    replay = store.claim(owner, item.id, item.digest)
    assert completed["result"] == {"id": "saved"}
    assert replay.status == "completed"
