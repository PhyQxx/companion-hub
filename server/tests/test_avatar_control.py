from app.avatar import control_from_agent_reply, with_reply_text


def test_agent_reply_maps_to_bounded_avatar_control() -> None:
    assert control_from_agent_reply(
        {
            "emotion": "happy",
            "expressions": ["smile", "ignored"],
            "actions": [
                {"type": "sound", "value": "ignored"},
                {"type": "animation", "value": "TapBody:1"},
            ],
        }
    ) == {
        "emotion": "happy",
        "expression": "smile",
        "motion": "TapBody:1",
    }


def test_invalid_agent_reply_produces_no_control() -> None:
    assert control_from_agent_reply(None) == {}
    assert control_from_agent_reply({"emotion": 123, "actions": ["bad"]}) == {}


def test_reply_text_is_compact_and_bounded() -> None:
    assert with_reply_text({"emotion": "happy"}, "  第一行\n  第二行  ") == {
        "emotion": "happy",
        "text": "第一行 第二行",
    }
    assert with_reply_text({}, "x" * 400)["text"] == "x" * 280
