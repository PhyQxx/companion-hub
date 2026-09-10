from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.config import HomeAssistantEntityConfig
from app.schemas import PrivacyLevel

from .models import HomeAssistantError


@dataclass(frozen=True, slots=True)
class DeviceDirectoryEntry:
    entity_id: str
    name: str
    room: str | None
    domain: str
    available: bool
    actions: tuple[str, ...]
    confirmation_required_actions: tuple[str, ...]
    match_kind: str


@dataclass(frozen=True, slots=True)
class DeviceSearchPage:
    devices: tuple[DeviceDirectoryEntry, ...]
    has_more: bool
    next_cursor: str | None


class DeviceDirectory:
    """Local index of configured HA policies; search never grants execution permission."""

    def __init__(self, policies: tuple[HomeAssistantEntityConfig, ...] = ()) -> None:
        self._policies = policies

    def rebuild(self, policies: tuple[HomeAssistantEntityConfig, ...]) -> None:
        self._policies = policies

    def search(
        self,
        *,
        privacy_level: PrivacyLevel,
        available: Callable[[str], bool],
        query: str | None = None,
        room: str | None = None,
        domain: str | None = None,
        action: str | None = None,
        limit: int = 5,
        cursor: str | None = None,
    ) -> DeviceSearchPage:
        offset = _decode_cursor(cursor)
        query_key = _normalize(query)
        room_key = _normalize(room)
        domain_key = _normalize(domain)
        rows: list[tuple[tuple[int, float, str], DeviceDirectoryEntry]] = []
        for policy in self._policies:
            required = PrivacyLevel(policy.privacy_level)
            if not policy.read_allowed or required is PrivacyLevel.L3:
                continue
            if _privacy_rank(privacy_level) < _privacy_rank(required):
                continue
            policy_domain = policy.entity_id.split(".", 1)[0]
            if room_key and _normalize(policy.room) != room_key:
                continue
            if domain_key and policy_domain.casefold() != domain_key:
                continue
            if action and action not in policy.allowed_actions:
                continue
            rank, similarity, match_kind = _match(policy, query_key)
            if query_key and rank >= 4 and similarity < 0.42:
                continue
            entry = DeviceDirectoryEntry(
                entity_id=policy.entity_id,
                name=policy.display_name,
                room=policy.room,
                domain=policy_domain,
                available=available(policy.entity_id),
                actions=tuple(policy.allowed_actions),
                confirmation_required_actions=tuple(policy.confirmation_required_actions),
                match_kind=match_kind,
            )
            rows.append(((rank, -similarity, policy.entity_id), entry))
        rows.sort(key=lambda item: item[0])
        page = rows[offset : offset + limit]
        next_offset = offset + len(page)
        has_more = next_offset < len(rows)
        return DeviceSearchPage(
            devices=tuple(item[1] for item in page),
            has_more=has_more,
            next_cursor=_encode_cursor(next_offset) if has_more else None,
        )


def _match(policy: HomeAssistantEntityConfig, query: str) -> tuple[int, float, str]:
    if not query:
        return 3, 1.0, "browse"
    entity = policy.entity_id.casefold()
    name = _normalize(policy.display_name)
    aliases = tuple(_normalize(value) for value in policy.aliases)
    if query == entity:
        return 0, 1.0, "entity_exact"
    if query == name:
        return 0, 1.0, "name_exact"
    if query in aliases:
        return 0, 1.0, "alias_exact"
    candidates = (entity, name, *aliases)
    if any(query in value or value in query for value in candidates):
        return 1, max(_similarity(query, value) for value in candidates), "substring"
    similarity = max(_similarity(query, value) for value in candidates)
    return 4, similarity, "similar"


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio()


def _normalize(value: str | None) -> str:
    return value.strip().casefold() if value else ""


def _privacy_rank(value: PrivacyLevel) -> int:
    return list(PrivacyLevel).index(value)


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = int(base64.urlsafe_b64decode(padded).decode())
        if value < 0:
            raise ValueError
        return value
    except (ValueError, UnicodeDecodeError) as error:
        raise HomeAssistantError("ha_search_cursor_invalid") from error
