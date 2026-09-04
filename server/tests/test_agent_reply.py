# ruff: noqa: RUF001
from app.chat.reply import (
    CONTROL_END,
    CONTROL_START,
    ControlStreamFilter,
    parse_agent_reply,
)
from app.persona import PersonaConfig


def test_structured_reply_is_parsed_and_control_is_not_visible() -> None:
    persona = PersonaConfig(expression_map={"happy": "smile"})
    raw = (
        "很高兴见到你！"
        f'{CONTROL_START}{{"schema_version":1,"emotion":"happy",'
        '"tts_text":"见到你真开心！","expressions":[],"actions":[]}'
        f"{CONTROL_END}"
    )

    reply = parse_agent_reply(raw, persona)

    assert reply.text == "很高兴见到你！"
    assert reply.tts_text == "见到你真开心！"
    assert reply.emotion == "happy"
    assert reply.expressions == ["smile"]
    assert reply.parse_status == "structured"


def test_stream_filter_handles_split_marker_without_delaying_normal_chunks() -> None:
    stream_filter = ControlStreamFilter()
    emitted: list[str] = []
    for chunk in ["你好", "，今天", "怎么样？<aria_", "control>{}", CONTROL_END]:
        emitted.extend(stream_filter.feed(chunk))
    emitted.extend(stream_filter.finish())

    assert "".join(emitted) == "你好，今天怎么样？"
    assert emitted[:2] == ["你好", "，今天"]


def test_malformed_control_safely_falls_back_to_visible_text() -> None:
    reply = parse_agent_reply(f"正常文本{CONTROL_START}not-json", PersonaConfig())

    assert reply.text == "正常文本"
    assert reply.parse_status == "fallback"


def test_control_only_reply_never_leaks_private_protocol() -> None:
    raw = (
        f'{CONTROL_START}{{"schema_version":1,"emotion":"happy",'
        '"tts_text":null,"expressions":[],"actions":[]}'
        f"{CONTROL_END}"
    )

    reply = parse_agent_reply(raw, PersonaConfig(expression_map={"happy": "smile"}))

    assert reply.text == "抱歉，我暂时没有生成有效回复。"
    assert CONTROL_START not in reply.text
    assert reply.emotion == "happy"
    assert reply.expressions == ["smile"]
    assert reply.parse_status == "fallback"
