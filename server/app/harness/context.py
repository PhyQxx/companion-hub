"""Deterministic context composition shared by conversation entry points.

Sources remain responsible for authorization, privacy filtering and retrieval.
This first extraction preserves prompt ordering and summary watermark semantics;
it deliberately does not introduce truncation or additional model calls.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.llm.contracts import LLMMessage


class ContextMessage(Protocol):
    @property
    def seq(self) -> int: ...

    @property
    def role(self) -> str: ...

    @property
    def content(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ContextBlocks:
    time: str
    reality: str
    action_catalog: str = ""
    recent_device: str = ""
    memory: str = ""
    screen_activity: str = ""
    browser_activity: str = ""
    history_recall: str = ""
    skill_guidance: str = ""
    unavailable_capability: str = ""


class ContextAssembler:
    def assemble(
        self,
        *,
        system_prompt: str,
        reply_instruction: str,
        history: Sequence[ContextMessage],
        blocks: ContextBlocks,
        conversation_summary: tuple[str, int] | None = None,
    ) -> list[LLMMessage]:
        summary = self.summary_block(history, conversation_summary)
        content = system_prompt + reply_instruction + f"\n\n{blocks.time}\n\n{blocks.reality}"
        for fragment in (
            blocks.action_catalog,
            blocks.recent_device,
            blocks.memory,
            blocks.screen_activity,
            blocks.browser_activity,
            blocks.history_recall,
            summary,
            blocks.skill_guidance,
            blocks.unavailable_capability,
        ):
            if fragment:
                content += f"\n\n{fragment}"
        messages = [LLMMessage(role="system", content=content)]
        for message in history:
            if message.role == "user":
                messages.append(LLMMessage(role="user", content=message.content))
            elif message.role == "assistant":
                messages.append(LLMMessage(role="assistant", content=message.content))
        return messages

    @staticmethod
    def summary_block(
        history: Sequence[ContextMessage],
        summary: tuple[str, int] | None,
    ) -> str:
        if summary is None or not history or history[0].seq <= 1:
            return ""
        if summary[1] < history[0].seq - 1:
            return ""
        return f"【此前对话要点（截至第 {summary[1]} 条消息）】\n{summary[0]}"
