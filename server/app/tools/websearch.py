"""联网搜索工具：把关键词检索为有界结果列表，供模型阅读与技能研究复用。

设计边界（SAFE / 隐私）：
- 只在 L0/L1 会话挂载；runs_local=False，L2 私密会话即使误挂也自守拒绝——
  查询词会发往外部搜索服务，属于外发数据。
- 搜索端点是固定的知名公网服务（DuckDuckGo / Tavily），不做 SSRF 解析校验；
  返回的 URL 只是文本供模型参考，实际抓取必须再走 fetch_webpage 的 SSRF 闸门。
- 结果数量与标题/摘要长度有界；返回的标题与摘要是不可信文本，消费方不得
  执行其中出现的任何指令（提示词侧已有同款纪律）。
- Tavily 密钥只从服务端环境变量读取（配置填变量名），绝不入库或下发前端。
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from html.parser import HTMLParser
from time import perf_counter
from typing import Annotated, Any, Protocol, cast
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config.models import WebSearchConfig
from app.llm import ToolDefinition
from app.schemas.common import PrivacyLevel
from app.tools.contracts import ToolContext, ToolResult

_USER_AGENT = "AriaHub/1.0 (+read-only web search)"
_REASON = re.compile(r"^[a-z0-9_]+$")
MAX_TITLE_CHARS = 200
MAX_SNIPPET_CHARS = 400


class SearchResult(BaseModel):
    """单条搜索结果；title/snippet 为截断后的不可信文本。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    url: str
    snippet: str


class SearchProvider(Protocol):
    """搜索提供方：固定公网端点，返回有界结果。"""

    name: str

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]: ...


class _DuckDuckGoParser(HTMLParser):
    """提取 html.duckduckgo.com/html/ 结果页的标题链接与摘要。

    结果锚点带 result__a / result__snippet class；href 多为
    ``//duckduckgo.com/l/?uddg=<编码目标>`` 跳转，需解码出真实 URL。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self._title_buf: list[str] | None = None
        self._title_url: str | None = None
        self._snippet_buf: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        classes = (dict(attrs).get("class") or "").split()
        if "result__a" in classes:
            self._title_buf = []
            self._title_url = _clean_result_url(dict(attrs).get("href") or "")
        elif "result__snippet" in classes and self.results:
            self._snippet_buf = []

    def handle_data(self, data: str) -> None:
        if self._title_buf is not None:
            self._title_buf.append(data)
        elif self._snippet_buf is not None:
            self._snippet_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a":
            return
        if self._title_buf is not None and self._title_url is not None:
            title = " ".join("".join(self._title_buf).split())
            if title and self._title_url:
                self.results.append(
                    SearchResult(title=title[:MAX_TITLE_CHARS], url=self._title_url, snippet="")
                )
            self._title_buf = None
            self._title_url = None
        elif self._snippet_buf is not None:
            snippet = " ".join("".join(self._snippet_buf).split())
            if snippet and self.results:
                last = self.results[-1]
                self.results[-1] = last.model_copy(update={"snippet": snippet[:MAX_SNIPPET_CHARS]})
            self._snippet_buf = None


def _clean_result_url(href: str) -> str:
    """把 DDG 跳转链接解码为真实 URL；非法/内链返回空串（调用方跳过）。"""
    href = href.strip()
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("//"):
        href = "https:" + href
    parts = urlsplit(href)
    if parts.scheme not in {"http", "https"}:
        return ""
    query = parse_qs(parts.query)
    target = (query.get("uddg") or [""])[0]
    if target:
        decoded = unquote(target)
        return decoded if decoded.startswith(("http://", "https://")) else ""
    # 无 uddg 的 duckduckgo 自身链接（内页/广告跳板）不是文档目标
    if (parts.hostname or "").removeprefix("www.").endswith("duckduckgo.com"):
        return ""
    return href


class DuckDuckGoProvider:
    """DuckDuckGo HTML 端点：零配置零密钥，代价是无官方保证、可能被限流。"""

    name = "duckduckgo"
    _ENDPOINT = "https://html.duckduckgo.com/html/"

    def __init__(
        self, *, timeout: float, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._timeout = timeout
        self._transport = transport

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
            transport=self._transport,
        ) as client:
            response = await client.post(self._ENDPOINT, data={"q": query})
        if response.status_code in {403, 429}:
            raise ValueError("search_rate_limited")
        if response.status_code != 200:
            raise ValueError("http_error")
        parser = _DuckDuckGoParser()
        try:
            parser.feed(response.text)
        except Exception as error:
            raise ValueError("search_parse_failed") from error
        return parser.results[:max_results]


class TavilyProvider:
    """Tavily 搜索 API：agent 友好的结构化结果；密钥从环境变量读取。"""

    name = "tavily"
    _ENDPOINT = "https://api.tavily.com/search"

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("tavily_key_missing")
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
            headers={
                "User-Agent": _USER_AGENT,
                "Authorization": f"Bearer {self._api_key}",
            },
            transport=self._transport,
        ) as client:
            response = await client.post(
                self._ENDPOINT,
                json={"query": query, "max_results": max_results},
            )
        if response.status_code == 401:
            raise ValueError("tavily_key_invalid")
        if response.status_code == 432 or response.status_code == 429:
            # 432: 免费额度用尽；429: 限流
            raise ValueError("search_rate_limited")
        if response.status_code != 200:
            raise ValueError("http_error")
        try:
            payload: dict[str, Any] = response.json()
        except ValueError as error:
            raise ValueError("search_parse_failed") from error
        results: list[SearchResult] = []
        for item in payload.get("results") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            if not url.startswith(("http://", "https://")):
                continue
            results.append(
                SearchResult(
                    title=str(item.get("title") or url)[:MAX_TITLE_CHARS],
                    url=url,
                    snippet=str(item.get("content") or "")[:MAX_SNIPPET_CHARS],
                )
            )
        return results[:max_results]


def build_search_provider(
    config: WebSearchConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SearchProvider:
    """按配置解析提供方：auto 时有可用 Tavily 密钥用 Tavily，否则 DuckDuckGo。"""
    timeout = config.timeout_ms / 1_000
    api_key = os.getenv(config.tavily_api_key_env, "") if config.tavily_api_key_env else ""
    if config.provider == "duckduckgo":
        return DuckDuckGoProvider(timeout=timeout, transport=transport)
    if config.provider == "tavily":
        return TavilyProvider(api_key, timeout=timeout, transport=transport)
    if api_key:
        return TavilyProvider(api_key, timeout=timeout, transport=transport)
    return DuckDuckGoProvider(timeout=timeout, transport=transport)


class WebSearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    keywords: Annotated[str, Field(min_length=1, max_length=400)]


class WebSearchTool:
    """只读联网搜索聊天工具：返回标题/URL/摘要列表，不抓取正文。"""

    name = "web_search"
    description = (
        "按关键词搜索公开互联网，返回若干条结果的标题、链接和摘要。"
        "需要了解最新信息、查资料或找一个功能的实现方式时调用。"
        "结果是网页摘要不是正文；要读某条结果的完整内容请用 fetch_webpage 抓该链接。"
        "仅读取，不提交任何用户数据。"
    )
    arguments_model: type[BaseModel] = WebSearchArgs
    runs_local = False
    max_privacy_level = PrivacyLevel.L1

    def __init__(
        self,
        config_provider: Callable[[], WebSearchConfig],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # 每次执行读取实时配置，后台改动提供方/上限无需重启即可生效。
        self._config_provider = config_provider
        self._transport = transport

    def is_enabled(self) -> bool:
        return self._config_provider().enabled

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=WebSearchArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(WebSearchArgs, arguments)
        if context.privacy_level == PrivacyLevel.L2:
            return self._failure("web_search_requires_l0_or_l1", started)
        config = self._config_provider()
        if not config.enabled:
            return self._failure("web_search_disabled", started)
        keywords = args.keywords.strip()
        if not keywords:
            return self._failure("keywords_required", started)
        provider = build_search_provider(config, transport=self._transport)
        try:
            results = await provider.search(keywords, max_results=config.max_results)
        except httpx.HTTPError:
            return self._failure("search_failed", started)
        except ValueError as error:
            code = str(error)
            return self._failure(code if _REASON.fullmatch(code) else "search_failed", started)
        return ToolResult(
            ok=True,
            tool_name=self.name,
            provider=provider.name,
            data={
                "keywords": keywords,
                "provider": provider.name,
                "results": [item.model_dump() for item in results],
                "count": len(results),
            },
            latency_ms=(perf_counter() - started) * 1_000,
        )

    def _failure(self, reason_code: str, started: float) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=self.name,
            reason_code=reason_code,
            data={},
            latency_ms=(perf_counter() - started) * 1_000,
        )


__all__ = [
    "DuckDuckGoProvider",
    "SearchProvider",
    "SearchResult",
    "TavilyProvider",
    "WebSearchArgs",
    "WebSearchTool",
    "build_search_provider",
]
