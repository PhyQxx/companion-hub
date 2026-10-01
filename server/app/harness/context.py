"""Deterministic context composition shared by conversation entry points.

Sources remain responsible for authorization, privacy filtering and retrieval.
Assembly preserves content; manifests contain source metadata only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Protocol

from app.llm.contracts import LLMContextPart, LLMMessage


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


@dataclass(frozen=True, slots=True)
class ContextReference:
    kind: str
    source_id: str
    owner_id: str
    privacy_level: str
    version: str | None = None
    parent_kind: str | None = None
    parent_id: str | None = None
    lineage: tuple[tuple[str, str], ...] = ()
    source_count: int | None = None
    included: bool = True
    reason: str = "authorized_snapshot"

    def manifest(self) -> dict[str, object]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class ContextSource:
    """Resolved source boundary; rendering never grants access to source data."""

    kind: str
    content: str
    owner_id: str
    privacy_ceiling: str
    version: str | None = None
    exclusion_reason: str = "empty_or_unavailable"
    references: tuple[ContextReference, ...] = ()

    def manifest(self) -> dict[str, object]:
        return {
            "source": self.kind,
            "references": [reference.manifest() for reference in self.references],
            "owner_id": self.owner_id,
            "privacy_ceiling": self.privacy_ceiling,
            "version": self.version,
            "included": bool(self.content),
            "reason": "protected_context" if self.content else self.exclusion_reason,
        }


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

    def source_manifest(
        self,
        *,
        blocks: ContextBlocks,
        history: Sequence[ContextMessage],
        conversation_summary: tuple[str, int] | None,
        owner_id: str,
        privacy_level: str,
        summary_exclusion_reason: str = "outside_watermark_or_absent",
        references: Mapping[str, tuple[ContextReference, ...]] | None = None,
    ) -> tuple[dict[str, object], ...]:
        sources = [
            ContextSource(
                kind=field.name,
                content=getattr(blocks, field.name),
                owner_id=owner_id,
                privacy_ceiling=privacy_level,
                references=(references or {}).get(field.name, ()),
            )
            for field in fields(blocks)
        ]
        profile = (references or {}).get("assistant_profile", ())
        sources.append(
            ContextSource(
                kind="assistant_profile",
                content="present" if profile else "",
                owner_id=owner_id,
                privacy_ceiling=privacy_level,
                references=profile,
            )
        )
        sources.append(
            ContextSource(
                kind="history",
                content="present" if history else "",
                owner_id=owner_id,
                privacy_ceiling=privacy_level,
                references=(references or {}).get("history", ()),
            )
        )
        sources.append(
            ContextSource(
                kind="conversation_summary",
                content=self.summary_block(history, conversation_summary),
                owner_id=owner_id,
                privacy_ceiling=privacy_level,
                version=str(conversation_summary[1]) if conversation_summary else None,
                exclusion_reason=summary_exclusion_reason,
                references=(references or {}).get("conversation_summary", ()),
            )
        )
        return tuple(source.manifest() for source in sources)

    def context_parts(
        self,
        *,
        system_prompt: str,
        reply_instruction: str,
        blocks: ContextBlocks,
        history: Sequence[ContextMessage],
        conversation_summary: tuple[str, int] | None,
    ) -> list[LLMContextPart]:
        # Only redundant old summary is optional. Directly requested recall and
        # grounded exact facts remain protected until richer relevance policy exists.
        parts = [
            LLMContextPart(
                source="policy",
                content=system_prompt
                + reply_instruction
                + f"\n\n{blocks.time}\n\n{blocks.reality}",
            )
        ]
        for name in (
            "action_catalog",
            "recent_device",
            "memory",
            "screen_activity",
            "browser_activity",
            "history_recall",
            "conversation_summary",
            "skill_guidance",
            "unavailable_capability",
        ):
            content = (
                self.summary_block(history, conversation_summary)
                if name == "conversation_summary"
                else getattr(blocks, name)
            )
            if content:
                parts.append(
                    LLMContextPart(
                        source=name,
                        content=f"\n\n{content}",
                        protected=name != "conversation_summary",
                        priority=0,
                    )
                )
        return parts

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
