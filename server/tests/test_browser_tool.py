# ruff: noqa: RUF001
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.devices import CommandSnapshot, DeviceSnapshot, EphemeralDeviceAssetStore
from app.ids import uuid7
from app.tools import ToolContext
from app.tools.browser import MAX_TOOL_PAGE_TEXT, InspectWebpageArgs, InspectWebpageTool
from app.tools.intent import select_device_tools
from app.tools.screen import ScreenAnalysis

PNG = b"\x89PNG\r\n\x1a\n" + b"browser-screen"


class FakeResolver:
    def __init__(self, device: DeviceSnapshot) -> None:
        self.device = device
        self.capability: str | None = None

    async def resolve(self, **kwargs: object) -> DeviceSnapshot:
        self.capability = str(kwargs["capability"])
        return self.device


class FakeAnalyzer:
    def __init__(self) -> None:
        self.seen: bytes | None = None

    async def analyze(self, **kwargs: object) -> ScreenAnalysis:
        self.seen = kwargs["data"]  # type: ignore[assignment]
        return ScreenAnalysis(text="页面显示一张系统架构图", provider="local_vision")


class FakeGateway:
    def __init__(self, *, owner_id: UUID, device_id: UUID) -> None:
        self.assets = EphemeralDeviceAssetStore()
        self.owner_id = owner_id
        self.device_id = device_id
        self.command_name: str | None = None

    async def issue(self, **kwargs: object) -> CommandSnapshot:
        self.command_name = str(kwargs["command"])
        now = datetime.now(UTC)
        command_id = uuid7()
        if self.command_name.endswith(".read"):
            media_type = "application/json"
            data = json.dumps(
                {
                    "title": "Companion Hub Docs",
                    "origin": "https://example.com",
                    "language": "zh-CN",
                    "text": "x" * (MAX_TOOL_PAGE_TEXT + 10),
                    "truncated": False,
                }
            ).encode()
        else:
            media_type = "image/png"
            data = PNG
        asset = await self.assets.put(
            owner_user_id=self.owner_id,
            device_id=self.device_id,
            command_id=command_id,
            media_type=media_type,
            data=data,
            now=now,
        )
        return CommandSnapshot(
            id=command_id,
            device_id=self.device_id,
            command_name=self.command_name,
            args_redacted={},
            idempotency_key="browser-tool-idempotency",
            status="succeeded",
            revision=3,
            issued_at=now,
            expires_at=now + timedelta(seconds=30),
            sent_at=now,
            acknowledged_at=now,
            completed_at=now,
            reason_code=None,
            result_meta={"asset_id": str(asset.id)},
        )

    async def wait_for_terminal(self, *_: object, **__: object) -> CommandSnapshot:
        raise AssertionError("already-terminal command must not wait")


def _device(owner_id: UUID, device_id: UUID) -> DeviceSnapshot:
    now = datetime.now(UTC)
    capabilities = ("browser.current_tab.read", "browser.current_tab.capture")
    return DeviceSnapshot(
        id=device_id,
        owner_user_id=owner_id,
        name="Chrome",
        alias="我的浏览器",
        client_type="browser",
        capabilities=capabilities,
        granted_capabilities=capabilities,
        effective_capabilities=capabilities,
        revision=1,
        paired_at=now,
        last_seen_at=now,
        revoked_at=None,
        online=True,
    )


async def test_inspect_webpage_reads_bounded_structured_text() -> None:
    owner_id = uuid7()
    device_id = uuid7()
    resolver = FakeResolver(_device(owner_id, device_id))
    gateway = FakeGateway(owner_id=owner_id, device_id=device_id)
    tool = InspectWebpageTool(resolver, gateway, FakeAnalyzer())

    result = await tool.execute(
        InspectWebpageArgs(),
        ToolContext(privacy_level="L2", user_id=owner_id, turn_id=uuid7()),
    )

    assert result.ok is True
    assert gateway.command_name == "browser.current_tab.read"
    assert resolver.capability == "browser.current_tab.read"
    assert result.data["origin"] == "https://example.com"
    assert len(result.data["visible_text"]) == MAX_TOOL_PAGE_TEXT
    assert result.data["truncated"] is True


async def test_inspect_webpage_capture_uses_local_vision() -> None:
    owner_id = uuid7()
    device_id = uuid7()
    resolver = FakeResolver(_device(owner_id, device_id))
    gateway = FakeGateway(owner_id=owner_id, device_id=device_id)
    analyzer = FakeAnalyzer()
    tool = InspectWebpageTool(resolver, gateway, analyzer)

    result = await tool.execute(
        InspectWebpageArgs(mode="capture", question="图里是什么？"),
        ToolContext(privacy_level="L2", user_id=owner_id, turn_id=uuid7()),
    )

    assert result.ok is True
    assert gateway.command_name == "browser.current_tab.capture"
    assert resolver.capability == "browser.current_tab.capture"
    assert analyzer.seen == PNG
    assert result.data["analysis"] == "页面显示一张系统架构图"


def test_webpage_intent_prefers_browser_over_screen_capture() -> None:
    selected = select_device_tools(
        "看一下我的电脑网页",
        (
            f"{uuid7()}:screen.capture",
            f"{uuid7()}:browser.current_tab.read",
        ),
    )

    assert selected == ("inspect_webpage",)
