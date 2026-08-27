# ruff: noqa: RUF001
"""聊天运行时的现实能力边界。

这里描述的不是“模型会什么”，而是当前 Hub 真的能在现实世界完成什么。
只有已连接、在线且已授权的设备/工具动作才应该进入该列表。聊天模型在
转移话题或提出现实活动建议时，只能引用这里列出的能力，避免虚构“去训练场、
去后院、一起做饭”等系统无法完成的行动。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


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


def render_reality_grounding(actions: Sequence[RuntimeActionCapability]) -> str:
    """把真实能力渲染成强约束系统提示。"""
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
    lines = "\n".join(
        f"- {item.capability_id}｜{item.label}：{item.description}" for item in actions
    )
    return (
        base
        + "\n当前可用现实能力：\n"
        + lines
        + "\n例如，只有列表里明确存在电视控制能力时，才可以建议‘我们看会儿电视吧，"
        "我可以帮你打开电视’。不得把角色设定中的场景当作真实能力。"
    )
