from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx
import pytest

from app.config.models import WebSearchConfig
from app.schemas import PrivacyLevel
from app.tools.contracts import ToolContext
from app.tools.websearch import (
    DuckDuckGoProvider,
    TavilyProvider,
    WebSearchArgs,
    WebSearchTool,
    build_search_provider,
)


def _config(**over: Any) -> WebSearchConfig:
    return WebSearchConfig(enabled=True, **over)


def _tool(handler: Any, *, config: WebSearchConfig | None = None) -> WebSearchTool:
    cfg = config or _config()
    return WebSearchTool(lambda: cfg, transport=httpx.MockTransport(handler))


async def _run(
    tool: WebSearchTool,
    args: WebSearchArgs,
    level: PrivacyLevel = PrivacyLevel.L1,
) -> Any:
    return await tool.execute(args, ToolContext(privacy_level=level))


_DDG_HTML = (
    "<html><body>"
    '<h2><a class="result__a" href="//duckduckgo.com/l/?uddg={doc}&amp;rut=x">官方文档</a></h2>'
    '<a class="result__snippet" href="//duckduckgo.com/l/?uddg={doc}">接口与教程摘要</a>'
    '<h2><a class="result__a" href="https://plain.example/guide">普通指南</a></h2>'
    '<a class="result__snippet" href="https://plain.example/guide">plain snippet</a>'
    "</body></html>"
)


@pytest.mark.asyncio
async def test_duckduckgo_results_decoded_and_bounded() -> None:
    doc_url = "https://docs.example.com/api/reference"
    body = _DDG_HTML.format(doc=quote(doc_url, safe=""))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "html.duckduckgo.com"
        return httpx.Response(200, headers={"content-type": "text/html"}, text=body)

    result = await _run(_tool(handler), WebSearchArgs(keywords="example api documentation"))
    assert result.ok and result.data["provider"] == "duckduckgo"
    urls = [item["url"] for item in result.data["results"]]
    assert doc_url in urls and "https://plain.example/guide" in urls
    first = next(item for item in result.data["results"] if item["url"] == doc_url)
    assert first["title"] == "官方文档"
    assert "接口与教程摘要" in first["snippet"]


@pytest.mark.asyncio
async def test_duckduckgo_rate_limited() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    result = await _run(_tool(handler), WebSearchArgs(keywords="anything"))
    assert not result.ok and result.reason_code == "search_rate_limited"


@pytest.mark.asyncio
async def test_duckduckgo_internal_links_skipped() -> None:
    body = (
        '<a class="result__a" href="//duckduckgo.com/?q=bad">内链</a>'
        '<a class="result__a" href="mailto:x@example.com">邮件</a>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    result = await _run(_tool(handler), WebSearchArgs(keywords="ignored"))
    assert result.ok and result.data["results"] == []


@pytest.mark.asyncio
async def test_tavily_results_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_TAVILY_KEY", "tvly-test")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.tavily.com"
        assert request.headers["authorization"] == "Bearer tvly-test"
        payload: dict[str, Any] = json.loads(request.content)
        assert payload["query"] == "官方文档"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Docs", "url": "https://docs.example.com/", "content": "摘要"}
                ]
            },
        )

    tool = _tool(
        handler,
        config=_config(provider="tavily", tavily_api_key_env="TEST_TAVILY_KEY"),
    )
    result = await _run(tool, WebSearchArgs(keywords="官方文档"))
    assert result.ok and result.data["provider"] == "tavily"
    assert result.data["results"][0]["url"] == "https://docs.example.com/"


@pytest.mark.asyncio
async def test_tavily_key_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_TAVILY_KEY", "tvly-test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    tool = _tool(
        handler,
        config=_config(provider="tavily", tavily_api_key_env="TEST_TAVILY_KEY"),
    )
    result = await _run(tool, WebSearchArgs(keywords="ignored"))
    assert not result.ok and result.reason_code == "tavily_key_invalid"


@pytest.mark.asyncio
async def test_disabled_and_privacy_gates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应触网
        raise AssertionError("network must not be reached")

    disabled = WebSearchTool(
        lambda: WebSearchConfig(enabled=False), transport=httpx.MockTransport(handler)
    )
    result = await _run(disabled, WebSearchArgs(keywords="ignored"))
    assert not result.ok and result.reason_code == "web_search_disabled"

    blocked = await _run(_tool(handler), WebSearchArgs(keywords="ignored"), level=PrivacyLevel.L2)
    assert not blocked.ok and blocked.reason_code == "web_search_requires_l0_or_l1"


def test_provider_auto_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_TAVILY_KEY", raising=False)
    fallback = build_search_provider(_config())
    assert isinstance(fallback, DuckDuckGoProvider)

    monkeypatch.setenv("TEST_TAVILY_KEY", "tvly-test")
    selected = build_search_provider(_config(tavily_api_key_env="TEST_TAVILY_KEY"))
    assert isinstance(selected, TavilyProvider)

    forced = build_search_provider(
        _config(provider="duckduckgo", tavily_api_key_env="TEST_TAVILY_KEY")
    )
    assert isinstance(forced, DuckDuckGoProvider)


@pytest.mark.asyncio
async def test_http_error_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    result = await _run(_tool(handler), WebSearchArgs(keywords="ignored"))
    assert not result.ok and result.reason_code == "http_error"


@pytest.mark.asyncio
async def test_max_results_respected() -> None:
    rows = "".join(
        f'<h2><a class="result__a" href="https://x.example/{index}">t{index}</a></h2>'
        for index in range(8)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=f"<html><body>{rows}</body></html>")

    result = await _run(
        _tool(handler, config=_config(max_results=3)), WebSearchArgs(keywords="ignored")
    )
    assert result.ok and result.data["count"] == 3
