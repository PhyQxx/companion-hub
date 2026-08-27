from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import Field

from app.schemas.common import StrictModel
from app.schemas.reply import Emotion

# 档案槽位 key 与记忆库助手事实的 fact_key 保持一致，便于提示词合并时对位覆盖
PROFILE_LABELS: dict[str, str] = {
    "profile.height": "身高",
    "profile.weight": "体重",
    "profile.measurements": "三围",
    "profile.birthday": "生日",
    "profile.name": "名字",
    "profile.nickname": "昵称",
    "preference.food": "喜欢的食物",
    "preference.drink": "喜欢的饮品",
}

_PROFILE_KEY = r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$"


class PersonaConfig(StrictModel):
    schema_version: Literal[1] = 1
    name: Annotated[str, Field(min_length=1, max_length=80)] = "Aria"
    identity: Annotated[str, Field(min_length=1, max_length=2_000)] = (
        "一个可靠、自然的个人陪伴助手"
    )
    system_prompt: Annotated[str, Field(min_length=1, max_length=20_000)] = (
        "直接回答用户，不要声称拥有未提供的记忆或能力。"
    )
    speaking_style: Annotated[str, Field(max_length=2_000)] = "自然、真诚、简洁"
    relationship: Annotated[str, Field(max_length=2_000)] = "尊重用户边界的长期伙伴"
    boundaries: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=lambda: ["不虚构记忆", "不虚构未具备的现实执行能力"], max_length=32
    )
    default_emotion: Emotion = "neutral"
    expression_map: dict[Emotion, Annotated[str, Field(min_length=1, max_length=80)]] = (
        Field(default_factory=dict)
    )
    voice_profile: Annotated[str, Field(max_length=160)] | None = None
    # 助手档案基线：身高/生日等稳定属性从这里出发，运行时被记忆库中
    # 用户明确告知的 active 事实按 fact_key 覆盖（见 ChatService.start_turn）
    profile: dict[
        Annotated[str, Field(pattern=_PROFILE_KEY)],
        Annotated[str, Field(min_length=1, max_length=200)],
    ] = Field(default_factory=dict, max_length=32)

    def render_system_prompt(self, profile_overrides: Mapping[str, str] | None = None) -> str:
        boundaries = "；".join(self.boundaries) or "无额外边界"
        profile = {**self.profile, **(profile_overrides or {})}
        prompt = (
            f"你是 {self.name}：{self.identity}。\n"
            f"核心要求：{self.system_prompt}\n"
            f"表达风格：{self.speaking_style}\n"
            f"与用户的关系：{self.relationship}\n"
            f"行为边界：{boundaries}"
        )
        if profile:
            lines = "；".join(
                f"{PROFILE_LABELS.get(key, key)}：{_profile_value(key, value)}"
                for key, value in profile.items()
            )
            prompt += (
                f"\n基本档案（自我描述必须与以下事实一致，"
                f"用户明确告知的最新值已经并入）：{lines}"
            )
        return prompt


def _profile_value(key: str, value: str) -> str:
    """去掉槽位值里重复的标签前缀，让「身高：身高为 170 厘米」收敛为「身高：170 厘米」。"""
    label = PROFILE_LABELS.get(key, key)
    stripped = value.strip().removeprefix("助手")
    for prefix in (f"{label}为", f"{label}是", f"{label}：", f"{label}:"):
        if stripped.startswith(prefix):
            return stripped[len(prefix) :].strip()
    return stripped
