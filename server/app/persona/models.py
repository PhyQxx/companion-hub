# ruff: noqa: RUF001
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from app.schemas.common import StrictModel
from app.schemas.reply import Emotion


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
        default_factory=lambda: ["不虚构记忆", "不冒充真人"], max_length=32
    )
    default_emotion: Emotion = "neutral"
    expression_map: dict[Emotion, Annotated[str, Field(min_length=1, max_length=80)]] = (
        Field(default_factory=dict)
    )
    voice_profile: Annotated[str, Field(max_length=160)] | None = None

    def render_system_prompt(self) -> str:
        boundaries = "；".join(self.boundaries) or "无额外边界"
        return (
            f"你是 {self.name}：{self.identity}。\n"
            f"核心要求：{self.system_prompt}\n"
            f"表达风格：{self.speaking_style}\n"
            f"与用户的关系：{self.relationship}\n"
            f"行为边界：{boundaries}"
        )
