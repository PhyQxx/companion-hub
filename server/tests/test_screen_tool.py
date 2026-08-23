# ruff: noqa: RUF001
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.devices import (
    CommandSnapshot,
    DeviceSnapshot,
    DeviceTargetAmbiguous,
    DeviceTargetCandidate,
    EphemeralDeviceAssetNotFound,
    EphemeralDeviceAssetStore,
)
from app.ids import uuid7
from app.llm import ToolCall
from app.tools import ToolContext, ToolExecutor, ToolRegistry
from app.tools.screen import (
    CaptureScreenArgs,
    CaptureScreenTool,
    ScreenAnalysis,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"screen-tool"


class FakeResolver:
    def __init__(self, device: DeviceSnapshot | Exception) -> None:
        self.device = device

    async def resolve(self, **_: object) -> DeviceSnapshot:
        if isinstance(self.device, Exception):
            raise self.device
        return self.device


class FakeAnalyzer:
    def __init__(self) -> None:
        self.seen: bytes | None = None

    async def analyze(self, **kwargs: object) -> ScreenAnalysis:
        self.seen = kwargs["data"]  # type: ignore[assignment]
        return ScreenAnalysis(text="屏幕显示 Aria 管理后台设备页", provider="local-vision")


class FakeGateway:
    def __init__(self, *, owner_id: UUID, device_id: UUID) -> None:
        self.assets = EphemeralDeviceAssetStore()
        self.owner_id = owner_id
        self.device_id = device_id
        self.asset_id: UUID | None = None
        self.command_id: UUID | None = None
        self.issued_args: dict[str, object] | None = None

    async def issue(self, **kwargs: object) -> CommandSnapshot:
        self.issued_args = kwargs["args"]  # type: ignore[assignment]
        now = datetime.now(UTC)
        command_id = uuid7()
        asset = await self.assets.put(
            owner_user_id=self.owner_id,
            device_id=self.device_id,
            command_id=command_id,
            media_type="image/png",
            data=PNG,
            now=now,
        )
        self.asset_id = asset.id
        self.command_id = command_id
        return CommandSnapshot(
            id=command_id,
            device_id=self.device_id,
            command_name="screen.capture",
            args_redacted={"target": "main_display"},
            idempotency_key="screen-test-idempotency",
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
    return DeviceSnapshot(
        id=device_id,
        owner_user_id=owner_id,
        name="MacBook Pro",
        alias="我的电脑",
        client_type="desktop",
        capabilities=("screen.capture",),
        granted_capabilities=("screen.capture",),
        effective_capabilities=("screen.capture",),
        revision=1,
        paired_at=now,
        last_seen_at=now,
        revoked_at=None,
        online=True,
    )


async def test_capture_screen_analyzes_and_consumes_ephemeral_image() -> None:
    owner_id = uuid7()
    device_id = uuid7()
    gateway = FakeGateway(owner_id=owner_id, device_id=device_id)
    analyzer = FakeAnalyzer()
    tool = CaptureScreenTool(
        FakeResolver(_device(owner_id, device_id)),
        gateway,
        analyzer,
    )
    turn_id = uuid7()

    execution = await ToolExecutor(ToolRegistry([tool])).execute(
        ToolCall(
            id="screen-call-1",
            function={
                "name": "capture_screen",
                "arguments": {"question": "页面上有哪些设备？"},
            },
        ),
        ToolContext(privacy_level="L2", user_id=owner_id, turn_id=turn_id),
    )

    assert execution.result.ok is True
    assert execution.result.data["analysis"] == "屏幕显示 Aria 管理后台设备页"
    assert gateway.issued_args == {"target": "main_display"}
    assert analyzer.seen == PNG
    assert gateway.asset_id is not None and gateway.command_id is not None
    with pytest.raises(EphemeralDeviceAssetNotFound):
        await gateway.assets.consume(
            gateway.asset_id,
            owner_user_id=owner_id,
            command_id=gateway.command_id,
        )


async def test_capture_screen_passes_bounded_display_index() -> None:
    owner_id = uuid7()
    device_id = uuid7()
    gateway = FakeGateway(owner_id=owner_id, device_id=device_id)
    tool = CaptureScreenTool(
        FakeResolver(_device(owner_id, device_id)),
        gateway,
        FakeAnalyzer(),
    )

    result = await tool.execute(
        CaptureScreenArgs(target="display", display_index=2),
        ToolContext(privacy_level="L2", user_id=owner_id, turn_id=uuid7()),
    )

    assert result.ok is True
    assert result.data["display_index"] == 2
    assert gateway.issued_args == {"target": "display", "display_index": 2}


def test_capture_screen_rejects_invalid_display_targets() -> None:
    invalid_arguments = (
        {"target": "display"},
        {"target": "display", "display_index": 0},
        {"target": "main_display", "display_index": 1},
    )
    for arguments in invalid_arguments:
        with pytest.raises(ValidationError):
            CaptureScreenArgs.model_validate(arguments)


async def test_capture_screen_returns_candidates_for_ambiguous_target() -> None:
    owner_id = uuid7()
    first = DeviceTargetCandidate(uuid7(), "Work Mac", "工作电脑", "desktop")
    second = DeviceTargetCandidate(uuid7(), "Home Mac", "家里电脑", "desktop")
    ambiguous = DeviceTargetAmbiguous("ambiguous", candidates=(first, second))
    gateway = FakeGateway(owner_id=owner_id, device_id=first.device_id)
    tool = CaptureScreenTool(
        FakeResolver(ambiguous),
        gateway,
        FakeAnalyzer(),
    )

    result = await tool.execute(
        CaptureScreenArgs(),
        ToolContext(privacy_level="L2", user_id=owner_id, turn_id=uuid7()),
    )

    assert result.reason_code == "device_target_ambiguous"
    candidates = result.data["candidates"]
    assert isinstance(candidates, list) and len(candidates) == 2


async def test_capture_screen_refuses_non_l2_context_before_device_access() -> None:
    owner_id = uuid7()
    device_id = uuid7()
    tool = CaptureScreenTool(
        FakeResolver(_device(owner_id, device_id)),
        FakeGateway(owner_id=owner_id, device_id=device_id),
        FakeAnalyzer(),
    )

    result = await tool.execute(
        CaptureScreenArgs(),
        ToolContext(privacy_level="L1", user_id=owner_id, turn_id=uuid7()),
    )

    assert result.reason_code == "screen_capture_requires_l2"
