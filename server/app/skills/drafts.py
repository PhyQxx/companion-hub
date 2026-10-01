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
from typing import Any, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.ids import uuid7
from app.llm import CompletionRequest, LLMMessage, LLMRoute, ToolDefinition
from app.schemas import PrivacyLevel
from app.tools.contracts import ToolContext, ToolResult
from app.tools.webfetch import FetchWebpageArgs, FetchWebpageTool

from .connections import SkillConnectionError, SkillConnectionStore, SkillHttpClient
from .generator import MAX_SOURCE_CHARS, CompletionBackend, SkillDraftGenerator
from .models import SkillApiManifest, SkillOperation
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
    target_skill: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="要修订的现有技能名；用户纠正已有技能的用法/接口时传入",
    )
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
        "用户纠正某个已有技能的用法或接口时，把该技能名传入 target_skill，"
        "草稿会成为该技能的新版本候选。"
        "仅当用户明确希望新增/修订能力时调用。草稿不会直接生效，需管理员审阅。"
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
        target = None
        if args.target_skill is not None:
            target = await self._store.get_by_name(args.target_skill)
            if target is None:
                return self._finish(False, started, "target_skill_not_found")
            # 修订不能悄悄切换连接：文档系统必须与现有技能的连接一致。
            if target.api is not None and target.api.connection != args.system_name:
                return self._finish(False, started, "revision_connection_mismatch")
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
            target_skill_id=target.id if target is not None else None,
            base_version=target.version if target is not None else None,
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
        if target is not None:
            return self._finish(
                True,
                started,
                data={
                    "status": draft.status,
                    "skill_name": target.name,
                    "revision_of": target.name,
                    "base_version": target.version,
                    "warnings": draft.warnings[:5],
                    "message": (
                        f"已生成 {target.name} 的修订草稿（基于 v{target.version}），"
                        "管理员在技能中心确认后会创建新版本。"
                    ),
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


async def verify_skill_draft(
    draft: SkillDraftView,
    *,
    store: SkillStore,
    connections: SkillConnectionStore,
    http_client: SkillHttpClient,
) -> SkillDraftView:
    """Run one live read-only GET of the draft contract before admin review.

    试跑走与运行时完全相同的连接白名单与认证闸门；结果只记录状态与
    原因码，不落任何响应内容。
    """
    if draft.target_skill_id is not None:
        return await _verify_revision(
            draft, store=store, connections=connections, http_client=http_client
        )
    ok, reason = await _probe_draft(draft, connections=connections, http_client=http_client)
    return await store.mark_draft_verified(draft.id, ok=ok, reason=reason)


class _ProbeResult(TypedDict):
    status: str
    reason: str | None


class _ContractCheck(TypedDict):
    operation: str
    before: _ProbeResult
    after: _ProbeResult


async def _verify_revision(
    draft: SkillDraftView,
    *,
    store: SkillStore,
    connections: SkillConnectionStore,
    http_client: SkillHttpClient,
) -> SkillDraftView:
    """Compare changed contracts with their immutable baseline, never raw responses.

    A successful unrelated GET cannot validate a revision. Operations needing
    user input, writes, and description-only changes remain inconclusive.
    This checks reachability only; it is not task replay or semantic evaluation.
    """
    assert draft.target_skill_id is not None
    target = await store.get(draft.target_skill_id)
    baseline = (
        next(
            (
                version
                for version in await store.versions(draft.target_skill_id)
                if version.version == draft.base_version
            ),
            None,
        )
        if target is not None
        else None
    )
    if target is None or baseline is None or target.version != draft.base_version:
        return await store.mark_draft_verified(
            draft.id,
            ok=False,
            reason="revision_baseline_stale",
            report={"scope": "changed_contracts", "outcome": "inconclusive", "checks": []},
        )
    old_api, new_api = baseline.api, draft.document.api
    old_ops = {item.name: item for item in old_api.operations} if old_api else {}
    new_ops = {item.name: item for item in new_api.operations} if new_api else {}
    auth_changed = bool(
        old_api
        and new_api
        and (old_api.auth != new_api.auth or old_api.connection != new_api.connection)
    )
    names = sorted(
        name
        for name in old_ops.keys() | new_ops.keys()
        if old_ops.get(name) != new_ops.get(name) or auth_changed
    )
    checks: list[_ContractCheck] = []
    for name in names:
        old, new = old_ops.get(name), new_ops.get(name)
        before = await _probe_operation(
            old_api,
            old,
            skill_id=draft.target_skill_id,
            connections=connections,
            http_client=http_client,
        )
        after = await _probe_operation(
            new_api,
            new,
            skill_id=draft.target_skill_id,
            connections=connections,
            http_client=http_client,
        )
        checks.append({"operation": name, "before": before, "after": after})
    # Missing/unsafe/parameterized checks are not evidence of improvement.
    statuses = [check["after"]["status"] for check in checks]
    comparable = [
        check
        for check in checks
        if check["before"]["status"] != "skipped" and check["after"]["status"] != "skipped"
    ]
    regressed = any(
        check["before"]["status"] == "passed" and check["after"]["status"] == "failed"
        for check in comparable
    )
    improved = any(
        check["before"]["status"] == "failed" and check["after"]["status"] == "passed"
        for check in comparable
    )
    complete = bool(statuses) and all(status == "passed" for status in statuses)
    if regressed:
        outcome = "regressed"
    elif not complete or len(comparable) != len(checks):
        outcome = "inconclusive"
    else:
        outcome = "improved" if improved else "unchanged"
    reason = None if complete else "revision_checks_incomplete"
    if "failed" in statuses:
        reason = "revision_probe_failed"
    return await store.mark_draft_verified(
        draft.id,
        ok=complete,
        reason=reason,
        report={
            "scope": "changed_contracts",
            "base_version": draft.base_version,
            "outcome": outcome,
            "checks": checks,
        },
    )


async def _probe_operation(
    api: SkillApiManifest | None,
    operation: SkillOperation | None,
    *,
    skill_id: UUID | None,
    connections: SkillConnectionStore,
    http_client: SkillHttpClient,
) -> _ProbeResult:
    if api is None or operation is None:
        return {"status": "skipped", "reason": "operation_absent"}
    if operation.risk != "read" or operation.method != "GET":
        return {"status": "skipped", "reason": "write_operation_not_probed"}
    if any(spec.required for spec in operation.parameters.values()):
        return {"status": "skipped", "reason": "operation_requires_input"}
    ok, reason = await _probe_contract(
        api, operation, skill_id=skill_id, connections=connections, http_client=http_client
    )
    return {"status": "passed" if ok else "failed", "reason": reason}


async def _probe_draft(
    draft: SkillDraftView,
    *,
    connections: SkillConnectionStore,
    http_client: SkillHttpClient,
) -> tuple[bool, str | None]:
    document = draft.document
    api = document.api
    if api is None:
        return False, "draft_has_no_api"
    reads = [item for item in api.operations if item.risk == "read"]
    if not reads:
        return False, "draft_has_no_read_operation"
    # 只试跑无需调用方供参的操作；带必填 path 参数的操作无法安全代填。
    operation = next(
        (item for item in reads if not any(spec.required for spec in item.parameters.values())),
        None,
    )
    if operation is None:
        return False, "draft_operation_needs_path_values"
    return await _probe_contract(
        api,
        operation,
        skill_id=draft.target_skill_id,
        connections=connections,
        http_client=http_client,
    )


async def _probe_contract(
    api: SkillApiManifest,
    operation: SkillOperation,
    *,
    skill_id: UUID | None,
    connections: SkillConnectionStore,
    http_client: SkillHttpClient,
) -> tuple[bool, str | None]:
    connection = await connections.get(api.connection)
    if connection is None or not connection.enabled:
        return False, "connection_disabled"
    if operation.path not in connection.allowed_paths:
        return False, "connection_path_denied"
    if connection.auth_type == "login_bearer":
        if api.auth is None or api.auth.path not in connection.allowed_auth_paths:
            return False, "connection_auth_path_denied"
        if not await http_client.credentials_ready(skill_id, connection):
            return False, "connection_credentials_missing"
    elif api.auth is not None:
        return False, "connection_auth_mismatch"
    try:
        await http_client.skill_get(
            api.connection,
            operation.path,
            operation.path,
            params={},
            auth=api.auth,
            skill_id=skill_id,
        )
    except SkillConnectionError as error:
        return False, error.reason_code
    except Exception:
        logger.warning("skill draft verification crashed", exc_info=True)
        return False, "draft_verification_failed"
    return True, None
