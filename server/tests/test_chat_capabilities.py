from __future__ import annotations

from app.chat import RuntimeActionCapability, render_reality_grounding


def test_reality_grounding_forbids_unavailable_physical_actions() -> None:
    prompt = render_reality_grounding([])

    assert "当前没有可用的现实设备/工具动作" in prompt
    assert "不要建议去训练场" in prompt
    assert "控制未接入设备" in prompt
    assert "身高、体重、三围" in prompt
    assert "我没有真实身体，所以没有这些数据" in prompt
    assert "不冒充真人" in prompt
    assert "不是要求否认角色自我档案" in prompt
    assert "角色化身体设定不等于现实世界的物理执行能力" in prompt


def test_reality_grounding_allows_only_reported_device_actions() -> None:
    prompt = render_reality_grounding(
        [
            RuntimeActionCapability(
                capability_id="device.tv.living_room.power",
                label="客厅电视",
                description="可以打开或关闭客厅电视，并切换到已授权的视频输入源",
            ),
            RuntimeActionCapability(
                capability_id="device.fan.bedroom.power",
                label="卧室电风扇",
                description="可以打开或关闭卧室电风扇",
            ),
        ]
    )

    assert "device.tv.living_room.power" in prompt
    assert "客厅电视" in prompt
    assert "device.fan.bedroom.power" in prompt
    assert "只有列表里明确存在电视控制能力时" in prompt
    assert "不得把角色设定中的场景当作真实能力" in prompt


def test_reality_grounding_keeps_home_device_catalog_out_of_on_demand_prompt() -> None:
    actions = [
        RuntimeActionCapability(
            capability_id=f"home_assistant:light.room_{index}:turn_on",
            label=f"房间灯 {index}",
            description="可以执行已授权的 Home Assistant 控制",
        )
        for index in range(200)
    ]

    prompt = render_reality_grounding(actions, home_device_mode="on_demand")

    assert "当前有 200 个已授权且在线的设备" in prompt
    assert "search_devices" in prompt
    assert "light.room_0" not in prompt
    assert "light.room_199" not in prompt
    assert len(prompt) < 2_500


def test_reality_grounding_compact_mode_groups_actions_per_device() -> None:
    actions = [
        RuntimeActionCapability(
            capability_id="home_assistant:light.bedroom:state.read",
            label="卧室灯",
            description="读取",
        ),
        RuntimeActionCapability(
            capability_id="home_assistant:light.bedroom:turn_on",
            label="卧室灯 · turn_on",
            description="控制",
        ),
    ]

    prompt = render_reality_grounding(actions, home_device_mode="compact")

    assert prompt.count("light.bedroom") == 1
    assert "state.read" in prompt
    assert "turn_on" in prompt
