from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .common import StrictModel

Emotion = Literal[
    "neutral", "happy", "sad", "angry", "surprised", "thinking", "concerned"
]


class AgentAction(StrictModel):
    type: Literal["expression", "animation", "sound", "picture"]
    value: Annotated[str, Field(min_length=1, max_length=160)]


class AgentReply(StrictModel):
    schema_version: Literal[1] = 1
    schema_ref: Literal["aria.agent-reply/1"] = "aria.agent-reply/1"
    text: Annotated[str, Field(min_length=1, max_length=20_000)]
    tts_text: Annotated[str, Field(min_length=1, max_length=20_000)]
    emotion: Emotion = "neutral"
    expressions: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=8
    )
    actions: list[AgentAction] = Field(default_factory=list, max_length=16)
    parse_status: Literal["structured", "fallback"] = "structured"
