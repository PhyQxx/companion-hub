from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from app.config.models import HubConfig, QueryToolConfig, ToolsConfig, WebSearchConfig
from app.ids import uuid7
from app.jobs.delegated import DelegCancelled, DelegHandlerError, DelegRunContext
from app.llm import LLMRoute
from app.llm.contracts import CompletionResult, ModelEndpoint, RoutePolicy
from app.schemas import PrivacyLevel
from app.skills.generator import SkillProposal
from app.skills.models import SkillDocument
from app.skills.research import (
    DELEG_KIND_SKILL_RESEARCH,
    ResearchSkillTool,
    SkillResearchHandler,
)
from app.tools.contracts import ToolContext, ToolResult

_SNIPPET_SENTINEL = "SECRET_SNIPPET_TEXT"


class _StaticStore:
    def __init__(self, config: HubConfig) -> None:
        self.current = SimpleNamespace(config=config)


class _FakeSearch:
    def __init__(self, results: list[dict[str, str]] | None = None, ok: bool = True) -> None:
        self._results = results if results is not None else _default_results()
        self._ok = ok

    async def execute(self, args: Any, context: ToolContext) -> ToolResult:
        if not self._ok:
            return _fail("search_failed")
        return ToolResult(
            ok=True,
            tool_name="web_search",
            provider="duckduckgo",
            data={"provider": "duckduckgo", "results": self._results},
            latency_ms=1.0,
        )


def _default_results() -> list[dict[str, str]]:
    return [
        {
            "title": "官方文档",
            "url": "https://docs.example.com/api",
            "snippet": _SNIPPET_SENTINEL,
        },
        {
            "title": "指南",
            "url": "https://guide.example.com/howto",
            "snippet": _SNIPPET_SENTINEL,
        },
    ]


def _fail(reason: str) -> ToolResult:
    return ToolResult(ok=False, tool_name="fake", reason_code=reason, data={}, latency_ms=1.0)


class _FakeFetch:
    def __init__(self, ok: bool = True) -> None:
        self._ok = ok

    async def execute(self, args: Any, context: ToolContext) -> ToolResult:
        if not self._ok:
            return _fail("http_error")
        return ToolResult(
            ok=True,
            tool_name="fetch_webpage",
            provider="web-fetch",
            data={"title": "文档", "text": "# 用法\n第一步 按官方文档操作"},
            latency_ms=1.0,
        )


class _FakeGenerator:
    def __init__(self, error: ValueError | None = None) -> None:
        self.error = error
        self.system_names: list[str] = []

    async def generate(
        self, source: str, *, system_name: str, filename: str | None = None
    ) -> SkillProposal:
        self.system_names.append(system_name)
        if self.error is not None:
            raise self.error
        return SkillProposal(
            document=SkillDocument(
                name="demo-skill", description="演示技能", instructions="按文档操作"
            ),
            warnings=["w1"],
            evidence=[],
            executable=False,
        )


class _FakeDraftStore:
    """save_draft 桩：draft=False 表示去重（返回 None），error 抛 ValueError。"""

    def __init__(self, *, draft: bool = True, error: ValueError | None = None) -> None:
        self._draft = draft
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def save_draft(self, proposal: Any, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if not self._draft:
            return None
        return SimpleNamespace(
            document=SkillDocument(
                name="demo-skill", description="演示技能", instructions="按文档操作"
            ),
            warnings=["w1"],
        )


class _FakeBackend:
    def __init__(self, responses: list[str] | None = None, crash: bool = False) -> None:
        self.responses = list(responses or [])
        self.crash = crash
        self.requests: list[Any] = []

    async def complete(self, request: Any) -> CompletionResult:
        self.requests.append(request)
        if self.crash:
            raise RuntimeError("backend down")
        text = self.responses.pop(0) if self.responses else "{}"
        return CompletionResult(
            text=text,
            provider="t",
            model="m",
            endpoint="t",
            route=LLMRoute.UTILITY,
            latency_ms=1.0,
            finish_reason="stop",
        )


def _hub_config(*, search_enabled: bool = True) -> HubConfig:
    endpoint = ModelEndpoint(
        provider="t",
        model="m",
        base_url="https://api.example.com",
        runs_local=False,
        max_privacy_level=PrivacyLevel.L1,
    )
    local = ModelEndpoint(
        provider="local",
        model="lm",
        base_url="http://127.0.0.1:1",
        runs_local=True,
        max_privacy_level=PrivacyLevel.L2,
    )
    return HubConfig(
        models={"t": endpoint, "local": local},
        routes={
            LLMRoute.DIALOGUE: RoutePolicy(primary="t"),
            LLMRoute.UTILITY: RoutePolicy(primary="t"),
            LLMRoute.PRIVATE: RoutePolicy(primary="local"),
        },
        tools=ToolsConfig(
            enabled=True,
            query=QueryToolConfig(enabled=False),
            web_search=WebSearchConfig(enabled=search_enabled),
        ),
    )


def _handler(
    *,
    search: Any = None,
    fetch: Any = None,
    generator: Any = None,
    store: Any = None,
    backend: Any = None,
    config: HubConfig | None = None,
) -> tuple[SkillResearchHandler, Any, Any, Any]:
    search_obj = search or _FakeSearch()
    fetch_obj = fetch or _FakeFetch()
    generator_obj = generator or _FakeGenerator()
    store_obj = store or _FakeDraftStore()
    backend_obj = backend or _FakeBackend(
        ['{"terms":["how to x docs"],"system_name":"demo-sys"}', '{"pick":[1]}']
    )
    handler = SkillResearchHandler(
        search=cast(Any, search_obj),
        fetch=cast(Any, fetch_obj),
        generator=cast(Any, generator_obj),
        store=cast(Any, store_obj),
        config_store=cast(Any, _StaticStore(config or _hub_config())),
        router_builder=lambda cfg: backend_obj,
    )
    return handler, search_obj, fetch_obj, backend_obj


def _run_context(cancelled: bool = False) -> DelegRunContext:
    async def _cancel() -> bool:
        return cancelled

    return DelegRunContext(job_id=uuid7(), is_cancel_requested=_cancel)


def _payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "user_id": str(uuid4()),
        "turn_id": str(uuid4()),
        "capability": "学会做 X",
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_full_chain_drafts_skill() -> None:
    handler, _, _, backend = _handler()
    result = await handler(_payload(), _run_context())
    assert result["outcome"] == "drafted"
    assert result["skill_name"] == "demo-skill"
    assert "草稿" in result["summary"] and "审阅" in result["summary"]
    assert result["documents"] == ["https://docs.example.com/api"]
    # 挑选请求只含标题与 URL，绝不包含搜索摘要正文（防提示注入）
    pick_user_content = backend.requests[1].messages[1].content
    assert "官方文档" in pick_user_content
    assert _SNIPPET_SENTINEL not in pick_user_content
    # 规划给出的 system_name 传递给生成器
    assert isinstance(handler._generator, _FakeGenerator)
    assert handler._generator.system_names == ["demo-sys"]


@pytest.mark.asyncio
async def test_draft_saved_with_research_source_and_owner() -> None:
    payload = _payload()
    store = _FakeDraftStore()
    handler, _, _, _ = _handler(store=store)
    await handler(payload, _run_context())
    call = store.calls[0]
    assert call["source"] == "research"
    assert call["system_name"] == "demo-sys"
    assert call["turn_id"] == payload["turn_id"]
    assert call["source_owner_id"] == UUID(payload["user_id"])
    assert call["allow_active_source"] is True


@pytest.mark.asyncio
async def test_no_search_results_reports_failure() -> None:
    handler, _, _, _ = _handler(search=_FakeSearch(results=[], ok=False))
    result = await handler(_payload(), _run_context())
    assert result["outcome"] == "failed"
    assert result["reason"] == "no_search_results"
    assert "no_search_results" in result["summary"]


@pytest.mark.asyncio
async def test_all_fetches_failed_reports_failure() -> None:
    handler, _, _, _ = _handler(fetch=_FakeFetch(ok=False))
    result = await handler(_payload(), _run_context())
    assert result["outcome"] == "failed"
    assert result["reason"] == "all_fetches_failed"
    assert result["failed"] and result["failed"][0]["reason"] == "http_error"


@pytest.mark.asyncio
async def test_generation_rejected_by_evidence_discipline() -> None:
    handler, _, _, _ = _handler(
        generator=_FakeGenerator(ValueError("generated_path_not_in_document"))
    )
    result = await handler(_payload(), _run_context())
    assert result["outcome"] == "failed"
    assert result["reason"] == "generated_path_not_in_document"


@pytest.mark.asyncio
async def test_duplicate_pending_draft() -> None:
    handler, _, _, _ = _handler(store=_FakeDraftStore(draft=False))
    result = await handler(_payload(), _run_context())
    assert result["outcome"] == "duplicate"
    assert result["reason"] == "duplicate_pending"


@pytest.mark.asyncio
async def test_source_fence_blocks_deleted_turn() -> None:
    handler, _, _, _ = _handler(store=_FakeDraftStore(error=ValueError("source_deleted")))
    result = await handler(_payload(), _run_context())
    assert result["outcome"] == "failed"
    assert result["reason"] == "source_deleted"


@pytest.mark.asyncio
async def test_cancelled_mid_research_raises() -> None:
    handler, _, _, _ = _handler()
    with pytest.raises(DelegCancelled):
        await handler(_payload(), _run_context(cancelled=True))


@pytest.mark.asyncio
async def test_search_disabled_config_rejected() -> None:
    handler, _, _, _ = _handler(config=_hub_config(search_enabled=False))
    with pytest.raises(DelegHandlerError) as exc:
        await handler(_payload(), _run_context())
    assert exc.value.reason_code == "web_search_disabled"


@pytest.mark.asyncio
async def test_invalid_payload_rejected() -> None:
    handler, _, _, _ = _handler()
    with pytest.raises(DelegHandlerError) as exc:
        await handler(_payload(capability="  "), _run_context())
    assert exc.value.reason_code == "payload_invalid"


@pytest.mark.asyncio
async def test_model_failure_falls_back_to_raw_capability() -> None:
    handler, _, _, _ = _handler(backend=_FakeBackend(crash=True))
    result = await handler(_payload(), _run_context())
    assert result["outcome"] == "drafted"
    assert result["search_terms"] == ["学会做 X"]


class _FakeEngine:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def submit(self, kind: str, payload: dict[str, Any], **kwargs: Any) -> Any:
        self.submitted.append((kind, payload, kwargs))
        return SimpleNamespace(id=uuid7(), status="queued")


def _tool_context(level: PrivacyLevel = PrivacyLevel.L1) -> ToolContext:
    return ToolContext(privacy_level=level, user_id=uuid4(), turn_id=uuid4())


@pytest.mark.asyncio
async def test_tool_submits_job_with_receipt() -> None:
    engine = _FakeEngine()
    tool = ResearchSkillTool(engine, lambda: True)  # type: ignore[arg-type]
    args = tool.arguments_model.model_validate(
        {"capability": "学会查汇率", "reason": "目前没有汇率工具"}
    )
    result = await tool.execute(args, _tool_context())
    assert result.ok and result.data["ack"]
    kind, payload, kwargs = engine.submitted[0]
    assert kind == DELEG_KIND_SKILL_RESEARCH
    assert payload["capability"] == "学会查汇率"
    assert kwargs["idempotency_key"].startswith("skill-research-")
    assert kwargs["source_turn_id"] is not None


@pytest.mark.asyncio
async def test_tool_gates_l2_disabled_and_context() -> None:
    engine = _FakeEngine()
    tool = ResearchSkillTool(engine, lambda: True)  # type: ignore[arg-type]
    args = tool.arguments_model.model_validate({"capability": "x", "reason": "r"})
    l2 = await tool.execute(
        args, ToolContext(privacy_level=PrivacyLevel.L2, user_id=uuid4(), turn_id=uuid4())
    )
    assert not l2.ok and l2.reason_code == "research_requires_l1"

    disabled = ResearchSkillTool(engine, lambda: False)  # type: ignore[arg-type]
    blocked = await disabled.execute(args, _tool_context())
    assert not blocked.ok and blocked.reason_code == "web_search_disabled"

    missing_turn = ToolContext(privacy_level=PrivacyLevel.L1, user_id=uuid4(), turn_id=None)
    no_turn = await tool.execute(args, missing_turn)
    assert not no_turn.ok and no_turn.reason_code == "turn_context_missing"

    bad_slug = tool.arguments_model.model_validate(
        {"capability": "x", "reason": "r", "system_name": "Bad Name"}
    )
    invalid = await tool.execute(bad_slug, _tool_context())
    assert not invalid.ok and invalid.reason_code == "invalid_system_name"
    assert engine.submitted == []
