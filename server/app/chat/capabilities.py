# ruff: noqa: RUF001
"""聊天运行时的现实能力边界。

这里描述的不是“模型会什么”，而是当前 Hub 真的能在现实世界完成什么。
只有已连接、在线且已授权的设备/工具动作才应该进入该列表。聊天模型在
转移话题或提出现实活动建议时，只能引用这里列出的能力，避免虚构“去训练场、
去后院、一起做饭”等系统无法完成的行动。
"""
from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from app.tools.intent import tools_for_capability

# 能服务任意 Home Assistant 能力的聊天工具集合（随 DEVICE_TOOL_REQUIREMENTS 同步推导）。
_HOME_ASSISTANT_TOOLS = frozenset(tools_for_capability("home_assistant:probe:action"))


@dataclass(frozen=True, slots=True)
class RuntimeActionCapability:
    """一个当前真实可用的现实动作能力。"""

    capability_id: str
    label: str
    description: str


class RuntimeCapabilityProvider(Protocol):
    """由未来的设备/工具注册表实现，只返回当下可用且已授权的动作。"""

    async def available_actions(self, user_id: UUID) -> Sequence[RuntimeActionCapability]: ...


class CompositeRuntimeCapabilityProvider:
    def __init__(self, providers: Iterable[RuntimeCapabilityProvider]) -> None:
        self._providers = tuple(providers)

    async def available_actions(self, user_id: UUID) -> Sequence[RuntimeActionCapability]:
        actions: dict[str, RuntimeActionCapability] = {}
        for provider in self._providers:
            for action in await provider.available_actions(user_id):
                actions[action.capability_id] = action
        return tuple(actions.values())


def _capability_gate_note(
    capability_id: str,
    mounted_tools: Collection[str],
    *,
    private_session_ready: bool,
) -> str | None:
    """能力在线但其聊天工具本轮未挂载时，返回给模型的边界标注；可用则返回 None。

    浏览器标签页读取在 L1/L2 会话开放（L0 公开模式与模型不支持工具调用时不挂载）。
    私有路由已就绪本地工具模型时，切换私密会话必定可用，因此给模型一条可执行的
    引导话术；其余情况只如实标注不可用，不承诺任何解锁方式。
    """
    serving = tools_for_capability(capability_id)
    if not serving or any(name in mounted_tools for name in serving):
        return None
    if "inspect_webpage" in serving and private_session_ready:
        return "当前会话未开放该工具；用户明确想看时，可引导切换到私密会话"
    return "当前会话未开放该工具；不得声称能使用或承诺执行"


def render_reality_grounding(
    actions: Sequence[RuntimeActionCapability],
    *,
    home_device_mode: Literal["full", "compact", "on_demand"] = "on_demand",
    mounted_device_tools: Collection[str] | None = None,
    private_session_ready: bool = False,
) -> str:
    """把真实能力渲染成强约束系统提示。

    mounted_device_tools 传本轮实际挂载的设备工具名集合；设备在线但其聊天工具
    未挂载的能力会被移入"当前会话未开放"分组，避免提示宣称的能力与会话内可用
    工具不一致。传 None 表示无挂载信息，按旧行为全部视为可用。
    """
    base = (
        "【现实能力边界】\n"
        "你是一个需要保持长期连续感的伴侣角色。你可以拥有并自然谈论稳定的角色化"
        "身体形象和个人档案，例如身高、体重、三围、外貌、生日、穿衣习惯和个人偏好；"
        "这些属于你的角色自我设定。用户询问这类信息时，应像一个熟悉的长期伴侣一样"
        "自然回答，不要仅因为运行载体是软件就用‘我没有真实身体，所以没有这些数据’"
        "来回避。如果已有关于助手自身的长期记忆，必须优先保持一致；如果还没有，允许"
        "结合 Persona 自然建立一个明确、可长期保持的自我事实，并在后续回合保持连续。\n"
        "如果 Persona 或历史配置里出现‘不冒充真人’之类边界，应把它理解为不得谎称"
        "自己是现实中的人类生物个体，而不是要求否认角色自我档案；它不限制你自然表达"
        "自己的身高、体重、三围、外貌和生活化设定。\n"
        "角色化身体设定不等于现实世界的物理执行能力。你不得声称自己此刻能亲自移动到"
        "某个现实地点、触碰物品，或完成系统没有接入的现实活动。若用户明确询问你的现实"
        "系统身份或是否具有真实人类生物身体，应保持诚实，不把角色设定冒充成现实硬件事实。\n"
        "当你想转移话题、建议一起做某件现实中的事，或承诺马上执行某个动作时，"
        "只能选择下方明确列出的当前可用能力。列表之外的现实动作一律不得提出为"
        "‘我们去做’或‘我来做’的建议。"
    )
    if not actions:
        return (
            base
            + "\n当前没有可用的现实设备/工具动作。你可以继续聊天、换一个纯对话话题，"
            "但不要建议去训练场、后院、厨房、商店等现实地点，也不要声称能做饭、训练、"
            "移动物品或控制未接入设备。这个限制不影响你自然谈论自己的角色身体设定、"
            "个人档案、喜好和生活化自我描述。"
        )
    home_actions = tuple(
        item for item in actions if item.capability_id.startswith("home_assistant:")
    )
    visible_actions = tuple(item for item in actions if item not in home_actions)
    available: list[RuntimeActionCapability] = []
    gated: list[tuple[RuntimeActionCapability, str]] = []
    for item in visible_actions:
        note = (
            None
            if mounted_device_tools is None
            else _capability_gate_note(
                item.capability_id,
                mounted_device_tools,
                private_session_ready=private_session_ready,
            )
        )
        if note is None:
            available.append(item)
        else:
            gated.append((item, note))
    lines = [
        f"- {item.capability_id}｜{item.label}：{item.description}" for item in available
    ]
    if gated:
        lines.append(
            "以下设备能力在线，但当前会话未开放对应的聊天工具，"
            "不得声称能使用、不得承诺执行："
        )
        lines.extend(
            f"- {item.capability_id}｜{item.label}：{item.description}（{note}）"
            for item, note in gated
        )
    ha_gated = (
        mounted_device_tools is not None
        and bool(home_actions)
        and not any(name in mounted_device_tools for name in _HOME_ASSISTANT_TOOLS)
    )
    if home_actions and home_device_mode == "full":
        lines.extend(
            f"- {item.capability_id}｜{item.label}：{item.description}" for item in home_actions
        )
    elif home_actions and home_device_mode == "compact":
        grouped: dict[str, tuple[str, set[str]]] = {}
        for item in home_actions:
            parts = item.capability_id.split(":")
            entity_id = parts[1]
            action = parts[2]
            label, entity_actions = grouped.setdefault(entity_id, (item.label, set()))
            entity_actions.add(action)
            grouped[entity_id] = (label, entity_actions)
        lines.extend(
            f"- {entity_id}｜{label}：{', '.join(sorted(entity_actions))}"
            for entity_id, (label, entity_actions) in sorted(grouped.items())
        )
    elif home_actions and not ha_gated:
        entity_count = len(
            {item.capability_id.split(":", 2)[1] for item in home_actions}
        )
        lines.append(
            f"- Home Assistant：当前有 {entity_count} 个已授权且在线的设备。"
            "设备名称、房间、可用动作以 search_devices 返回为准；名称明确时可直接调用状态、"
            "历史或控制工具，匹配失败或有歧义时先检索。不得编造设备，控制前后仍须校验权限、"
            "确认要求和实际状态。"
        )
    elif home_actions:
        entity_count = len(
            {item.capability_id.split(":", 2)[1] for item in home_actions}
        )
        lines.append(
            f"- Home Assistant：当前有 {entity_count} 个已授权且在线的设备，"
            "但当前会话未开放 Home Assistant 工具，不得读取、控制或承诺操作这些设备。"
        )
    if ha_gated and home_device_mode in {"full", "compact"}:
        lines.append("注意：当前会话未开放 Home Assistant 工具，上述设备动作不可执行，不得承诺。")
    return (
        base
        + "\n当前可用现实能力：\n"
        + "\n".join(lines)
        + "\n例如，只有列表里明确存在电视控制能力时，才可以建议‘我们看会儿电视吧，"
        "我可以帮你打开电视’。不得把角色设定中的场景当作真实能力。"
    )
