from __future__ import annotations

import socket
from typing import Any

import httpx
import pytest

from app.config.models import WebFetchConfig
from app.schemas import PrivacyLevel
from app.tools import webfetch as webfetch_module
from app.tools.contracts import ToolContext
from app.tools.webfetch import FetchWebpageTool, _assert_public_url


def _config(**over: Any) -> WebFetchConfig:
    return WebFetchConfig(enabled=True, **over)


def _tool(handler: Any, *, config: WebFetchConfig | None = None) -> FetchWebpageTool:
    cfg = config or _config()
    return FetchWebpageTool(lambda: cfg, transport=httpx.MockTransport(handler))


async def _run(
    tool: FetchWebpageTool, url: str, level: PrivacyLevel = PrivacyLevel.L1
) -> Any:
    args = tool.arguments_model.model_validate({"url": url})
    return await tool.execute(args, ToolContext(privacy_level=level))


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    # 传输层被 Mock，跳过真实 DNS 校验；SSRF 逻辑在下方单独测试。
    monkeypatch.setattr(webfetch_module, "_assert_public_url", lambda url: None)


@pytest.mark.asyncio
async def test_fetch_html_extracts_visible_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "poetry.example"
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><head><title>诗集</title><script>evil()</script></head>"
                "<body><h1>你好</h1><p>世界</p></body></html>"
            ),
        )

    result = await _run(_tool(handler), "https://poetry.example/")
    assert result.ok
    assert result.data["title"] == "诗集"
    assert "你好" in result.data["text"] and "世界" in result.data["text"]
    assert "evil()" not in result.data["text"]
    assert result.data["truncated"] is False


@pytest.mark.asyncio
async def test_text_is_truncated_to_max_chars() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="字" * 5000)

    result = await _run(
        _tool(handler, config=_config(max_chars=1000)), "https://poetry.example/big.txt"
    )
    assert result.ok and result.data["truncated"] is True
    assert result.data["char_count"] == 1000


@pytest.mark.asyncio
async def test_response_too_large_aborts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x" * 5000)

    result = await _run(
        _tool(handler, config=_config(max_bytes=1024)), "https://poetry.example/big"
    )
    assert not result.ok and result.reason_code == "response_too_large"


@pytest.mark.asyncio
async def test_redirect_is_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "/new"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="landed")

    result = await _run(_tool(handler), "https://poetry.example/old")
    assert result.ok and result.data["text"] == "landed"
    assert result.data["url"].endswith("/new")


@pytest.mark.asyncio
async def test_too_many_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/loop"})

    result = await _run(
        _tool(handler, config=_config(max_redirects=2)), "https://poetry.example/loop"
    )
    assert not result.ok and result.reason_code == "too_many_redirects"


@pytest.mark.asyncio
async def test_http_error_reports_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    result = await _run(_tool(handler), "https://poetry.example/missing")
    assert not result.ok and result.reason_code == "http_error"
    assert result.data["status"] == 404


@pytest.mark.asyncio
async def test_unsupported_content_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4"
        )

    result = await _run(_tool(handler), "https://poetry.example/file.bin")
    assert not result.ok and result.reason_code == "content_type_unsupported"


@pytest.mark.asyncio
async def test_l2_session_refused() -> None:
    result = await _run(
        _tool(lambda request: httpx.Response(200, text="x")),
        "https://poetry.example/",
        level=PrivacyLevel.L2,
    )
    assert not result.ok and result.reason_code == "web_fetch_requires_l0_or_l1"


@pytest.mark.asyncio
async def test_ssrf_reason_is_surfaced(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str) -> None:
        raise ValueError("internal_address_blocked")

    monkeypatch.setattr(webfetch_module, "_assert_public_url", boom)
    result = await _run(_tool(lambda request: httpx.Response(200, text="x")), "https://x/")
    assert not result.ok and result.reason_code == "internal_address_blocked"


def test_scheme_and_credentials_rejected() -> None:
    with pytest.raises(ValueError, match="scheme_not_allowed"):
        _assert_public_url("ftp://example.com/x")
    with pytest.raises(ValueError, match="invalid_url"):
        _assert_public_url("https://user:pass@example.com")


def test_private_ip_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(ValueError, match="internal_address_blocked"):
        _assert_public_url("https://evil.example/")


def test_public_ip_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    _assert_public_url("https://example.com/")


def test_dns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(ValueError, match="dns_resolution_failed"):
        _assert_public_url("https://nonexistent.invalid/")
