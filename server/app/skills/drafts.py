"""Propose reviewable Skill drafts from live conversations.

Two entry points share one pipeline (generate -> save_draft -> admin review):
- ``propose_skill`` chat tool: the model proposes when the user hands over
  API documentation (pasted text or a URL fetched server-side via the
  read-only web fetch tool) or explicitly asks to persist a workflow as a skill.
- background harvest: after each committed turn, a cheap regex gate followed
  by a private-route extraction pass detects document-bearing requests that
  no enabled skill covers.

Drafts are never executable; approving one creates a disabled Skill through
the existing ``SkillStore.create`` review path.
"""

from __future__ import annotations

import json
import logging
import re
from time import perf_counter
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.ids import uuid7
from app.llm import CompletionRequest, LLMMessage, LLMRoute, ToolDefinition
from app.schemas import PrivacyLevel
from app.tools.contracts import ToolContext, ToolResult
from app.tools.webfetch import FetchWebpageArgs, FetchWebpageTool

from .generator import MAX_SOURCE_CHARS, CompletionBackend, SkillDraftGenerator
from .store import SkillDraftView, SkillStore

logger = logging.getLogger(__name__)

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DOC_MARKERS = re.compile(
    r"(?i)\b(GET|POST|PUT|PATCH|DELETE)\s+/[A-Za-z0-9_./{}-]+|接口文档|API\s*文档|openapi|swagger"
)
_HARVEST_SOURCE_CHARS = 8_000


class _ProposeSkillArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    system_name: str = Field(min_length=1, max_length=64, description="英文小写连字符系统标识")
    url: str | None = Field(
        default=None,
        min_length=1,
        max_length=2000,
        description="用户消息中给出的文档链接；有链接时传此项，不要转述网页正文",
    )
    source: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_SOURCE_CHARS,
        description="用户直接粘贴的接口文档原文；仅在用户未给链接时使用",
    )
    reason: str = Field(min_length=1, max_length=500, description="为什么值得沉淀成技能")


class SkillDraftAssistant:
    """Chat tool handler plus background harvester for skill drafts."""

    name = "propose_skill"
    description = (
        "把对话中提供的外部系统接口文档沉淀为待审阅的 Skill 草稿。"
        "用户给文档链接时把 url 原样传入（服务端自行抓取，勿转述正文）；"
        "用户直接粘贴文档文本时才把原文放入 source。"
        "仅当用户明确希望新增/记住某个外部系统能力时调用。"
        "草稿不会直接生效，需管理员审阅。"
    )
    arguments_model: type[BaseModel] = _ProposeSkillArgs
    max_privacy_level = PrivacyLevel.L2
    runs_local = True

    def __init__(
        self,
        store: SkillStore,
        generator: SkillDraftGenerator,
        *,
        web_fetch: FetchWebpageTool | None = None,
    ) -> None:
        self._store = store
        self._generator = generator
        self._web_fetch = web_fetch

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.arguments_model.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args: _ProposeSkillArgs = arguments  # type: ignore[assignment]
        if not _SLUG.fullmatch(args.system_name):
            return self._finish(False, started, "invalid_system_name")
        source = args.source
        if args.url is not None:
            # 链接由服务端原样抓取，避免模型转述正文造成校验失败或幻觉。
            fetched = await self._fetch_source(args.url, context)
            if isinstance(fetched, ToolResult):
                return fetched
            source = fetched
        if source is None or not source.strip():
            return self._finish(False, started, "source_required")
        try:
            proposal = await self._generator.generate(source, system_name=args.system_name)
        except ValueError as error:
            code = str(error)
            reason = code if re.fullmatch(r"[a-z0-9_]+", code) else "draft_generation_failed"
            return self._finish(False, started, reason)
        except Exception:
            logger.warning(
                "skill draft generation failed system=%s turn=%s",
                args.system_name,
                context.turn_id,
                exc_info=True,
            )
            return self._finish(False, started, "draft_generation_failed")
        draft = await self._store.save_draft(
            proposal,
            system_name=args.system_name,
            source="chat",
            turn_id=str(context.turn_id) if context.turn_id is not None else None,
        )
        if draft is None:
            return self._finish(
                True,
                started,
                data={
                    "status": "duplicate_pending",
                    "skill_name": proposal.document.name,
                    "message": "已有相同内容的待审阅草稿，无需重复提交。",
                },
            )
        return self._finish(
            True,
            started,
            data={
                "status": draft.status,
                "skill_name": draft.document.name,
                "warnings": draft.warnings[:5],
                "message": "已生成待审阅草稿，管理员在技能中心确认后才会启用。",
            },
        )

    async def _fetch_source(self, url: str, context: ToolContext) -> str | ToolResult:
        """Fetch documentation text for a URL, or a failure ToolResult."""
        started = perf_counter()
        if self._web_fetch is None or not self._web_fetch.is_enabled():
            return self._finish(False, started, "web_fetch_disabled")
        if context.privacy_level == PrivacyLevel.L2:
            return self._finish(False, started, "web_fetch_requires_l0_or_l1")
        result = await self._web_fetch.execute(FetchWebpageArgs(url=url), context)
        if not result.ok:
            return self._finish(False, started, result.reason_code)
        text = str(result.data.get("text") or "")
        if not text.strip():
            return self._finish(False, started, "documentation_empty")
        return text

    async def harvest(
        self,
        *,
        text: str,
        turn_id: UUID | None,
        backend: CompletionBackend,
    ) -> SkillDraftView | None:
        """Detect document-bearing requests after a turn and draft a skill."""
        if not _DOC_MARKERS.search(text):
            return None
        extraction = await self._extract(text, backend=backend)
        if extraction is None:
            return None
        system_name, source = extraction
        if source.strip() not in text:
            # 提取结果必须逐字出自用户消息，防止模型补全不存在的接口。
            return None
        return await self._propose(
            source, system_name=system_name, source_kind="harvest", turn_id=turn_id
        )

    async def _extract(self, text: str, *, backend: CompletionBackend) -> tuple[str, str] | None:
        result = await backend.complete(
            CompletionRequest(
                trace_id=uuid7(),
                messages=[
                    LLMMessage(
                        role="system",
                        content=(
                            "你是 Skill 沉淀分析器。判断用户消息是否包含某个外部系统的"
                            "API 接口文档，且值得沉淀为可复用技能。只输出 JSON："
                            '{"propose":true,"system_name":英文小写连字符标识,'
                            '"source":从用户消息中逐字摘录的接口文档片段}'
                            '或 {"propose":false}。'
                            "不得改写、补全或翻译文档内容；闲聊、无接口细节时输出 false。"
                        ),
                    ),
                    LLMMessage(role="user", content=text[:_HARVEST_SOURCE_CHARS]),
                ],
                privacy_level=PrivacyLevel.L2,
                route=LLMRoute.PRIVATE,
                temperature=0,
                json_mode=True,
                max_tokens=4_096,
            )
        )
        if result.finish_reason == "length":
            return None
        try:
            raw: dict[str, Any] = json.loads(result.text)
        except json.JSONDecodeError:
            return None
        if not raw.get("propose"):
            return None
        system_name = str(raw.get("system_name", ""))
        source = str(raw.get("source", ""))
        if not _SLUG.fullmatch(system_name) or len(system_name) > 64:
            return None
        if not source.strip() or len(source) > MAX_SOURCE_CHARS:
            return None
        return system_name, source

    async def _propose(
        self,
        source: str,
        *,
        system_name: str,
        source_kind: str,
        turn_id: UUID | None,
    ) -> SkillDraftView | None:
        try:
            proposal = await self._generator.generate(source, system_name=system_name)
        except ValueError as error:
            logger.info(
                "skill draft generation rejected (%s) system=%s turn=%s",
                error,
                system_name,
                turn_id,
            )
            return None
        except Exception:
            logger.warning(
                "skill draft generation failed system=%s turn=%s",
                system_name,
                turn_id,
                exc_info=True,
            )
            return None
        return await self._store.save_draft(
            proposal,
            system_name=system_name,
            source=source_kind,
            turn_id=str(turn_id) if turn_id is not None else None,
        )

    @staticmethod
    def _finish(
        ok: bool,
        started: float,
        reason_code: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> ToolResult:
        return ToolResult(
            ok=ok,
            tool_name="propose_skill",
            provider="skills",
            data=data or {},
            reason_code=reason_code,
            latency_ms=(perf_counter() - started) * 1_000,
        )
