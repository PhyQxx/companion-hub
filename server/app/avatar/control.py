from __future__ import annotations

from typing import Protocol
from uuid import UUID

from pydantic import JsonValue


class AvatarControlPublisher(Protocol):
    async def publish_avatar_control(
        self, owner_user_id: UUID, control: dict[str, JsonValue]
    ) -> int: ...


def control_from_agent_reply(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        return {}
    control: dict[str, JsonValue] = {}
    emotion = value.get("emotion")
    if isinstance(emotion, str) and emotion:
        control["emotion"] = emotion[:80]
    expressions = value.get("expressions")
    if isinstance(expressions, list) and expressions and isinstance(expressions[0], str):
        control["expression"] = expressions[0][:160]
    actions = value.get("actions")
    if isinstance(actions, list):
        for action in actions:
            if (
                isinstance(action, dict)
                and action.get("type") == "animation"
                and isinstance(action.get("value"), str)
            ):
                control["motion"] = action["value"][:160]
                break
    return control


def with_reply_text(
    control: dict[str, JsonValue], content: str, *, limit: int = 280
) -> dict[str, JsonValue]:
    compact = " ".join(content.split())
    if not compact:
        return control
    return {**control, "text": compact[:limit]}
