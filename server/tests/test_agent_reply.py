# ruff: noqa: RUF001
from app.chat.reply import (
    CONTROL_END,
    CONTROL_START,
    ControlStreamFilter,
    parse_agent_reply,
)
from app.llm.text_tool_calls import extract_text_tool_calls
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


def test_text_tool_calls_are_extracted_and_stripped() -> None:
    raw = (
        '好嘞，先记媳妇。<contact_save>{"display_name":"秦晓雪",'
        '"relationship":"媳妇（妻子）"}</contact_save> 然后再记女儿。'
    )

    text, calls = extract_text_tool_calls(raw, {"contact_save", "contact_query"})

    assert text == "好嘞，先记媳妇。 然后再记女儿。"
    assert calls == [
        ("contact_save", {"display_name": "秦晓雪", "relationship": "媳妇（妻子）"})
    ]


def test_text_tool_call_with_invalid_json_is_stripped_but_not_a_call() -> None:
    raw = "<contact_save>{oops</contact_save>已收到。"

    text, calls = extract_text_tool_calls(raw, {"contact_save"})

    assert text == "已收到。"
    assert calls == []


def test_unrelated_and_unmounted_tags_are_left_alone() -> None:
    raw = '对比 <not_a_tool>{"x":1}</not_a_tool> 与 <contact_save>{"a":1}</contact_save>'

    text, calls = extract_text_tool_calls(raw, {"mail_read"})

    assert text == raw
    assert calls == []


def test_parse_agent_reply_strips_tool_tags_from_persisted_text() -> None:
    raw = (
        "已经记下啦。"
        '<contact_save>{"display_name":"秦晓雪"}</contact_save>'
        f'{CONTROL_START}{{"schema_version":1,"emotion":"happy",'
        '"tts_text":null,"expressions":[],"actions":[]}'
        f"{CONTROL_END}"
    )

    reply = parse_agent_reply(raw, PersonaConfig(), tool_names=("contact_save",))

    assert reply.text == "已经记下啦。"
    assert "<contact_save>" not in reply.text
    assert reply.parse_status == "structured"


def test_stream_filter_suppresses_mounted_tool_tags_and_keeps_later_text() -> None:
    stream_filter = ControlStreamFilter(extra_tags=("contact_save",))
    emitted: list[str] = []
    for chunk in [
        "先记一下。",
        "<cont",
        'act_save>{"display_name":"秦晓雪"}</contact',
        "_save>",
        "记好啦！",
        f'{CONTROL_START}{{"schema_version":1,"emotion":"neutral",'
        '"tts_text":null,"expressions":[],"actions":[]}'
        f"{CONTROL_END}",
    ]:
        emitted.extend(stream_filter.feed(chunk))
    emitted.extend(stream_filter.finish())

    assert "".join(emitted) == "先记一下。记好啦！"


def test_stream_filter_swallows_unterminated_control_block_to_end_of_stream() -> None:
    stream_filter = ControlStreamFilter(extra_tags=("contact_save",))
    emitted: list[str] = []
    for chunk in ["正文", f"{CONTROL_START}{{broken", "以及之后的一切"]:
        emitted.extend(stream_filter.feed(chunk))
    emitted.extend(stream_filter.finish())

    assert "".join(emitted) == "正文"
