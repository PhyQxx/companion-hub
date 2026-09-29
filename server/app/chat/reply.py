from __future__ import annotations

import json
from collections.abc import Collection
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from app.llm.text_tool_calls import extract_text_tool_calls
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


def parse_agent_reply(
    raw: str,
    persona: PersonaConfig,
    *,
    tool_names: Collection[str] = (),
) -> AgentReply:
    if tool_names:
        # 提交前兜底：即便上游已吸收文本形态工具调用，也不允许内部
        # 协议标签沉淀进聊天历史——历史会回灌后续轮次的上下文。
        raw, _ = extract_text_tool_calls(raw, tool_names)
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
    """Streams visible text while suppressing control blocks and tool tags.

    ``<aria_control>`` 按协议只出现在回复末尾，命中后吞掉到流结束；
    文本形态工具标签（标签名为本轮挂载的工具名）命中后吞到对应闭合
    标签为止，之后继续正常放行——模型可能在标签后再补正文。
    """

    def __init__(self, extra_tags: Collection[str] = ()) -> None:
        # close 为 None 表示吞掉到流结束（aria_control 的既有语义）。
        self._tags: dict[str, str | None] = {"<aria_control>": None}
        for name in extra_tags:
            if name:
                self._tags[f"<{name}>"] = f"</{name}>"
        self._max_open_length = max(len(open_tag) for open_tag in self._tags)
        self._buffer = ""
        self._close_tag: str | None = None
        self._swallow_rest = False

    def feed(self, delta: str) -> list[str]:
        if not delta or self._swallow_rest:
            return []
        self._buffer += delta
        emitted: list[str] = []
        while True:
            if self._close_tag is not None:
                end = self._buffer.find(self._close_tag)
                if end < 0:
                    return emitted
                self._buffer = self._buffer[end + len(self._close_tag) :]
                self._close_tag = None
                continue
            located = self._locate_open()
            if located is None:
                retained = self._partial_open_suffix()
                safe_length = len(self._buffer) - retained
                visible = self._buffer[:safe_length]
                self._buffer = self._buffer[safe_length:]
                if visible:
                    emitted.append(visible)
                return emitted
            open_at, open_tag, close_tag = located
            visible = self._buffer[:open_at]
            self._buffer = self._buffer[open_at + len(open_tag) :]
            if close_tag is None:
                self._swallow_rest = True
                self._buffer = ""
            else:
                self._close_tag = close_tag
            if visible:
                emitted.append(visible)

    def finish(self) -> list[str]:
        if self._close_tag is not None or self._swallow_rest or not self._buffer:
            self._buffer = ""
            return []
        visible = self._buffer
        self._buffer = ""
        return [visible]

    def _locate_open(self) -> tuple[int, str, str | None] | None:
        best: tuple[int, str, str | None] | None = None
        for open_tag, close_tag in self._tags.items():
            found = self._buffer.find(open_tag)
            if found >= 0 and (best is None or found < best[0]):
                best = (found, open_tag, close_tag)
        return best

    def _partial_open_suffix(self) -> int:
        # 保留可能是某个待出现标签前缀的尾部（如 "<contact_s"），其余放行。
        limit = min(len(self._buffer), self._max_open_length - 1)
        for size in range(limit, 0, -1):
            suffix = self._buffer[-size:]
            if any(open_tag.startswith(suffix) for open_tag in self._tags):
                return size
        return 0
