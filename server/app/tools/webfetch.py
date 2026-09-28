"""只读网页抓取聊天工具：把用户贴出的 URL 正文取回、转为有界文本供模型阅读。

设计边界（SAFE / 隐私）：
- 只在 L0/L1 会话挂载；runs_local=False，L2 私密会话即使误挂也自守拒绝。
- 出站前对主机做 DNS 解析，任一解析结果落在内网/回环/链路本地/保留段即拒绝，防 SSRF。
- 重定向逐跳手动跟随并重新校验，避免 302 把请求导到内网。
- 响应体按 max_bytes 截断即中止，正文按 max_chars 截断并标注 truncated。
- 只读取，不执行任何脚本/资源；HTML 仅做标签剥离。
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import socket
from collections.abc import Callable
from html.parser import HTMLParser
from time import perf_counter
from typing import Annotated, Any, ClassVar, cast
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config.models import WebFetchConfig
from app.llm import ToolDefinition
from app.schemas.common import PrivacyLevel
from app.tools.contracts import ToolContext, ToolResult

_USER_AGENT = "AriaHub/1.0 (+read-only web fetch)"
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
_TEXT_CONTENT_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/javascript",
    "application/manifest+json",
    "image/svg+xml",
)
_TEXT_SUFFIXES = (".md", ".txt", ".json", ".yaml", ".yml", ".html", ".htm", ".xml", ".csv")
_REASON = re.compile(r"^[a-z0-9_]+$")


class FetchWebpageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: Annotated[str, Field(min_length=1, max_length=2000)]


class _TextExtractor(HTMLParser):
    """剥离标签，跳过 script/style/noscript，保留可见文本与标题。"""

    _SKIP: ClassVar[set[str]] = {"script", "style", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        if data.strip():
            self.parts.append(data)


def _assert_public_url(url: str) -> None:
    """校验 scheme 并在连接前用 DNS 解析阻断内网地址（同步，放线程里跑）。"""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ValueError("scheme_not_allowed")
    host = parts.hostname
    if not host or parts.username or parts.password:
        raise ValueError("invalid_url")
    try:
        infos = socket.getaddrinfo(host, parts.port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        raise ValueError("dns_resolution_failed") from error
    for info in infos:
        raw = str(info[4][0])
        try:
            ip = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            raise ValueError("dns_resolution_failed") from None
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError("internal_address_blocked")


def _extract_text(
    content_type: str, url: str, data: bytes, max_chars: int
) -> tuple[str, str, bool]:
    """返回 (title, text, truncated)。仅处理文本/HTML，其它类型抛 unsupported。"""
    ct = content_type.lower()
    is_html = "html" in ct
    path = urlsplit(url).path.lower()
    if not is_html and not any(marker in ct for marker in _TEXT_CONTENT_TYPES):
        if not path.endswith(_TEXT_SUFFIXES):
            raise ValueError("content_type_unsupported")
        is_html = path.endswith((".html", ".htm"))
    charset = "utf-8"
    if "charset=" in ct:
        candidate = ct.split("charset=", 1)[1].split(";", 1)[0].strip().strip('"')
        if candidate:
            charset = candidate
    try:
        raw = data.decode(charset, errors="replace")
    except LookupError:
        raw = data.decode("utf-8-sig", errors="replace")
    title = ""
    if is_html:
        parser = _TextExtractor()
        with contextlib.suppress(Exception):
            parser.feed(raw)
        title = parser.title.strip()
        text = "\n".join(parser.parts)
    else:
        text = raw
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    truncated = len(text) > max_chars
    return title, text[:max_chars], truncated


class FetchWebpageTool:
    name = "fetch_webpage"
    description = (
        "读取用户提供的 http/https 网页或文档链接，返回其正文文本（有界截断）。"
        "当用户贴出一个 URL 并希望你据此回答、总结或沉淀技能时调用；仅读取，不提交任何数据。"
    )
    arguments_model: type[BaseModel] = FetchWebpageArgs
    runs_local = False
    max_privacy_level = PrivacyLevel.L1

    def __init__(
        self,
        config_provider: Callable[[], WebFetchConfig],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # 每次执行读取实时配置，后台改动超时/大小上限无需重启即可生效。
        self._config_provider = config_provider
        self._transport = transport

    def is_enabled(self) -> bool:
        return self._config_provider().enabled

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=FetchWebpageArgs.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        started = perf_counter()
        args = cast(FetchWebpageArgs, arguments)
        if context.privacy_level == PrivacyLevel.L2:
            return self._failure("web_fetch_requires_l0_or_l1", started)
        config = self._config_provider()
        url = args.url.strip()
        timeout = config.timeout_ms / 1_000
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                headers={"User-Agent": _USER_AGENT},
                transport=self._transport,
            ) as client:
                for _ in range(config.max_redirects + 1):
                    await asyncio.to_thread(_assert_public_url, url)
                    response = await client.send(client.build_request("GET", url), stream=True)
                    try:
                        if response.status_code in _REDIRECT_STATUS:
                            location = response.headers.get("location")
                            if not location:
                                return self._failure("redirect_without_location", started)
                            url = str(response.url.join(location))
                            continue
                        if response.status_code != 200:
                            return self._failure(
                                "http_error", started, data={"status": response.status_code}
                            )
                        content_type = response.headers.get("content-type", "")
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.aiter_bytes():
                            chunks.append(chunk)
                            total += len(chunk)
                            if total > config.max_bytes:
                                return self._failure("response_too_large", started)
                        body = b"".join(chunks)
                    finally:
                        await response.aclose()
                    title, text, truncated = _extract_text(
                        content_type, str(response.url), body, config.max_chars
                    )
                    if not text.strip():
                        return self._failure("empty_content", started)
                    return ToolResult(
                        ok=True,
                        tool_name=self.name,
                        data={
                            "url": str(response.url),
                            "status": response.status_code,
                            "content_type": content_type,
                            "title": title,
                            "text": text,
                            "truncated": truncated,
                            "char_count": len(text),
                        },
                        latency_ms=(perf_counter() - started) * 1_000,
                    )
                return self._failure("too_many_redirects", started)
        except httpx.HTTPError:
            return self._failure("fetch_failed", started)
        except ValueError as error:
            code = str(error)
            return self._failure(code if _REASON.fullmatch(code) else "fetch_failed", started)

    def _failure(
        self, reason_code: str, started: float, *, data: dict[str, Any] | None = None
    ) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=self.name,
            reason_code=reason_code,
            data=data or {},
            latency_ms=(perf_counter() - started) * 1_000,
        )


__all__ = ["FetchWebpageTool"]
