from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from app.persona import PersonaConfig
from app.schemas.common import StrictModel
from app.schemas.reply import AgentReply, Emotion

CONTROL_START = "<aria_control>"
CONTROL_END = "</aria_control>"


class AgentControl(StrictModel):
    schema_version: Literal[1] = 1
    emotion: Emotion = "neutral"
    tts_text: Annotated[str, Field(min_length=1, max_length=20_000)] | None = None
    expressions: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=8
    )
    actions: list[dict[str, str]] = Field(default_factory=list, max_length=16)


def structured_reply_instruction(persona: PersonaConfig) -> str:
    emotions = "neutral, happy, sad, angry, surprised, thinking, concerned"
    return (
        "\n\n输出协议：先输出给用户看的正常回复；紧接着追加且只追加一个控制块："
        f'{CONTROL_START}{{"schema_version":1,"emotion":"neutral",'
        f'"tts_text":null,"expressions":[],"actions":[]}}{CONTROL_END}。'
        f"emotion 只能是：{emotions}。控制块不能放在 Markdown 代码块内。"
        "tts_text 仅在朗读文本确实需要和字幕不同时填写，否则为 null。"
        "不要在正常回复中解释这个协议。"
        f"未确定情绪时使用 {persona.default_emotion}。"
    )


def parse_agent_reply(raw: str, persona: PersonaConfig) -> AgentReply:
    visible, payload = _split_control(raw)
    if payload is not None:
        try:
            control = AgentControl.model_validate_json(payload)
            text = visible.strip()
            if text:
                expressions = list(control.expressions)
                mapped = persona.expression_map.get(control.emotion)
                if mapped and mapped not in expressions:
                    expressions.insert(0, mapped)
                return AgentReply(
                    text=text,
                    tts_text=control.tts_text or text,
                    emotion=control.emotion,
                    expressions=expressions,
                    actions=control.actions,
                )
            # A valid control block without user-visible text is still an invalid
            # reply. Never fall back to the raw value here, otherwise the private
            # control protocol leaks into chat history and the UI.
            fallback = "抱歉，我暂时没有生成有效回复。"
            mapped = persona.expression_map.get(control.emotion)
            return AgentReply(
                text=fallback,
                tts_text=fallback,
                emotion=control.emotion,
                expressions=[mapped] if mapped else [],
                actions=control.actions,
                parse_status="fallback",
            )
        except ValidationError:
            pass

    # Also accept a complete AgentReply JSON object for non-streaming integrations.
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            candidate = AgentReply.model_validate(value)
            return candidate
    except (json.JSONDecodeError, ValidationError):
        pass

    fallback = visible.strip() or raw.strip()
    if not fallback:
        fallback = "抱歉，我暂时没有生成有效回复。"
    mapped = persona.expression_map.get(persona.default_emotion)
    return AgentReply(
        text=fallback,
        tts_text=fallback,
        emotion=persona.default_emotion,
        expressions=[mapped] if mapped else [],
        parse_status="fallback",
    )


def _split_control(raw: str) -> tuple[str, str | None]:
    start = raw.find(CONTROL_START)
    if start < 0:
        return raw, None
    end = raw.find(CONTROL_END, start + len(CONTROL_START))
    visible = raw[:start]
    if end < 0:
        return visible, None
    payload = raw[start + len(CONTROL_START) : end]
    return visible, payload


class ControlStreamFilter:
    """Streams visible text while retaining and suppressing the final control block."""

    def __init__(self) -> None:
        self._buffer = ""
        self._in_control = False

    def feed(self, delta: str) -> list[str]:
        if not delta or self._in_control:
            return []
        self._buffer += delta
        marker_at = self._buffer.find(CONTROL_START)
        if marker_at >= 0:
            visible = self._buffer[:marker_at]
            self._buffer = ""
            self._in_control = True
            return [visible] if visible else []
        retained = 0
        max_prefix = min(len(self._buffer), len(CONTROL_START) - 1)
        for size in range(max_prefix, 0, -1):
            if self._buffer.endswith(CONTROL_START[:size]):
                retained = size
                break
        safe_length = len(self._buffer) - retained
        visible = self._buffer[:safe_length]
        self._buffer = self._buffer[safe_length:]
        return [visible] if visible else []

    def finish(self) -> list[str]:
        if self._in_control or not self._buffer:
            return []
        visible = self._buffer
        self._buffer = ""
        return [visible]
