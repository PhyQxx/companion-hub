"""Short-lived, user-bound previews for mutations requiring an explicit UI click."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4


@dataclass
class PendingMutation:
    id: UUID
    user_id: UUID
    turn_id: UUID
    kind: str
    content: dict[str, Any]
    preview: dict[str, Any]
    digest: str
    expires_at: datetime
    status: str = "pending"
    result: dict[str, Any] | None = None

    def view(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "kind": self.kind,
            "preview": self.preview,
            "digest": self.digest,
            "expires_at": self.expires_at.isoformat(),
            "status": self.status,
            "result": self.result,
        }


class PendingMutationStore:
    """Single-process preview store; mutations are claimed before the first await."""

    def __init__(self, *, limit: int = 256) -> None:
        self._limit = limit
        self._items: dict[UUID, PendingMutation] = {}

    def prepare(
        self,
        *,
        user_id: UUID,
        turn_id: UUID,
        kind: str,
        content: dict[str, Any],
        preview: dict[str, Any],
        now: datetime | None = None,
    ) -> PendingMutation:
        current = now or datetime.now(UTC)
        self._prune(current)
        digest = sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for item in self._items.values():
            if item.user_id == user_id and item.turn_id == turn_id and item.kind == kind:
                if item.digest == digest:
                    return item
                if item.status == "pending":
                    item.status = "cancelled"
        if len(self._items) >= self._limit:
            raise OverflowError("confirmation_preview_capacity")
        item = PendingMutation(
            id=uuid4(),
            user_id=user_id,
            turn_id=turn_id,
            kind=kind,
            content=content,
            preview=preview,
            digest=digest,
            expires_at=current + timedelta(minutes=15),
        )
        self._items[item.id] = item
        return item

    def list(self, user_id: UUID, *, now: datetime | None = None) -> list[dict[str, Any]]:
        current = now or datetime.now(UTC)
        self._prune(current)
        return [item.view() for item in self._items.values() if item.user_id == user_id]

    def claim(self, user_id: UUID, item_id: UUID, digest: str) -> PendingMutation:
        item = self._owned(user_id, item_id)
        if digest != item.digest:
            raise ValueError("confirmation_preview_changed")
        if item.status == "completed":
            return item
        if item.status != "pending":
            raise ValueError("confirmation_preview_not_pending")
        item.status = "saving"
        return item

    def complete(self, item: PendingMutation, result: dict[str, Any]) -> dict[str, Any]:
        item.result = result
        item.status = "completed"
        return item.view()

    def mark_unknown(self, item: PendingMutation) -> None:
        item.status = "unknown_outcome"

    def cancel(self, user_id: UUID, item_id: UUID) -> dict[str, Any]:
        item = self._owned(user_id, item_id)
        if item.status != "pending":
            raise ValueError("confirmation_preview_not_pending")
        item.status = "cancelled"
        return item.view()

    def _owned(self, user_id: UUID, item_id: UUID) -> PendingMutation:
        item = self._items.get(item_id)
        if item is None or item.user_id != user_id:
            raise LookupError("confirmation_preview_not_found")
        if item.expires_at <= datetime.now(UTC):
            raise ValueError("confirmation_preview_expired")
        return item

    def _prune(self, now: datetime) -> None:
        self._items = {
            key: item
            for key, item in self._items.items()
            if item.expires_at > now or item.status == "saving"
        }


__all__ = ["PendingMutation", "PendingMutationStore"]
