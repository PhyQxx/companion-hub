from app.avatar import control_from_agent_reply


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
