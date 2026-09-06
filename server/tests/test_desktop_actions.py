"""PC-01 桌面白名单动作：配置校验、工具白名单门禁、动作注册与挂载隔离。"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.cognition.action_registry import (
    ActionRisk,
    ConfirmationPolicy,
    build_builtin_action_registry,
)
from app.config import DesktopActionsConfig, HubConfig
from app.schemas.common import PrivacyLevel
from app.tools.contracts import ToolContext
from app.tools.desktop_actions import (
    DesktopClipboardWriteTool,
    DesktopOpenAppTool,
    DesktopOpenUrlTool,
    DesktopSetVolumeTool,
    split_url,
)

DEVICE_ID = UUID("00000000-0000-0000-0000-00000000abc1")


def _config(**overrides: Any) -> DesktopActionsConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "allowed_apps": ["Safari", "VS Code"],
        "allow_volume": True,
        "allow_clipboard": True,
    }
    values.update(overrides)
    return DesktopActionsConfig.model_validate(values)


class FakeResolver:
    def __init__(self, *, runtime_error: bool = False) -> None:
        self.runtime_error = runtime_error
        self.requested: list[dict[str, Any]] = []

    async def resolve(
        self, *, owner_user_id: UUID, target: str | UUID | None, capability: str
    ) -> Any:
        self.requested.append(
            {
                "owner_user_id": owner_user_id,
                "target": target,
                "capability": capability,
            }
        )
        if self.runtime_error:
            raise RuntimeError("no device")
        return type("Device", (), {"id": DEVICE_ID})()


class FakeCommand:
    def __init__(self, status: str, reason_code: str | None = None) -> None:
        self.id = uuid4()
        self.status = status
        self.reason_code = reason_code


class FakeGateway:
    def __init__(self, terminal: FakeCommand) -> None:
        self.terminal = terminal
        self.issued: list[dict[str, Any]] = []

    async def issue(
        self,
        *,
        device_id: UUID,
        command: str,
        args: dict[str, Any],
        idempotency_key: str,
        ttl_seconds: int,
    ) -> FakeCommand:
        self.issued.append(
            {
                "device_id": device_id,
                "command": command,
                "args": args,
                "idempotency_key": idempotency_key,
                "ttl_seconds": ttl_seconds,
            }
        )
        return FakeCommand("succeeded")

    async def wait_for_terminal(
        self,
        command_id: UUID,
        *,
        timeout_seconds: float | None = None,
    ) -> FakeCommand:
        return self.terminal


def _context(
    *,
    privacy_level: PrivacyLevel = PrivacyLevel.L1,
    idempotency_key: str | None = "plan-step-1",
) -> ToolContext:
    return ToolContext(
        privacy_level=privacy_level,
        user_id=UUID("00000000-0000-0000-0000-00000000abc2"),
        turn_id=None,
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# 配置规范化
# ---------------------------------------------------------------------------


def test_config_normalizes_lists_casefold_and_dedupe() -> None:
    config = DesktopActionsConfig.model_validate(
        {
            "enabled": True,
            "allowed_apps": [" Safari ", "safari", "VS Code"],
            "allowed_url_hosts": ["GitHub.com", "github.com"],
        }
    )
    assert config.allowed_apps == ["safari", "vs code"]
    assert config.allowed_url_hosts == ["github.com"]
    assert config.allowed_url_schemes == ["http", "https"]
    assert config.enabled is True
    assert config.allow_volume is False
    assert config.allow_clipboard is False


def test_config_rejects_invalid_entries() -> None:
    with pytest.raises(ValidationError):
        DesktopActionsConfig.model_validate({"allowed_url_schemes": ["no scheme!"]})
    with pytest.raises(ValidationError):
        DesktopActionsConfig.model_validate({"allowed_url_schemes": ["1 bad"]})


def test_split_url_lowercases_scheme_and_host() -> None:
    assert split_url("HTTPS://GitHub.COM/x") == ("https", "github.com")
    assert split_url("ftp://example.com") == ("ftp", "example.com")
    assert split_url("not a url") == ("", "")
    assert split_url("") == ("", "")


# ---------------------------------------------------------------------------
# 工具白名单门禁
# ---------------------------------------------------------------------------


async def test_open_app_requires_whitelist_match() -> None:
    gateway = FakeGateway(FakeCommand("succeeded"))
    tool = DesktopOpenAppTool(FakeResolver(), gateway, lambda: _config())
    blocked = await tool.execute(
        type("Args", (), {"app": "Calculator", "target": None})(), _context()
    )
    assert not blocked.ok and blocked.reason_code == "desktop_action_not_allowed"
    assert gateway.issued == []

    disabled = DesktopOpenAppTool(
        FakeResolver(), gateway, lambda: _config(enabled=False, allowed_apps=["Safari"])
    )
    result = await disabled.execute(
        type("Args", (), {"app": "Safari", "target": None})(), _context()
    )
    assert not result.ok and result.reason_code == "desktop_action_not_allowed"

    allowed = await tool.execute(
        type("Args", (), {"app": "vs code", "target": None})(), _context()
    )
    assert allowed.ok
    assert gateway.issued[0]["command"] == "desktop.open_app"
    assert gateway.issued[0]["args"] == {"app": "vs code"}
    assert gateway.issued[0]["idempotency_key"] == "plan-step-1"


async def test_open_url_enforces_scheme_and_optional_host_allowlist() -> None:
    gateway = FakeGateway(FakeCommand("succeeded"))
    restricted = lambda: _config(allowed_url_hosts=["github.com"])  # noqa: E731
    tool = DesktopOpenUrlTool(FakeResolver(), gateway, restricted)

    bad_scheme = await tool.execute(
        type("Args", (), {"url": "ftp://github.com/x", "target": None})(), _context()
    )
    assert not bad_scheme.ok and bad_scheme.reason_code == "desktop_action_not_allowed"

    bad_host = await tool.execute(
        type("Args", (), {"url": "https://evil.example.com", "target": None})(), _context()
    )
    assert not bad_host.ok and bad_host.reason_code == "desktop_action_not_allowed"

    ok = await tool.execute(
        type("Args", (), {"url": "https://GITHUB.com/aria", "target": None})(), _context()
    )
    assert ok.ok
    assert gateway.issued[0]["command"] == "desktop.open_url"

    open_hosts = DesktopOpenUrlTool(FakeResolver(), gateway, lambda: _config())
    any_host = await open_hosts.execute(
        type("Args", (), {"url": "https://example.org/doc", "target": None})(), _context()
    )
    assert any_host.ok


async def test_volume_and_clipboard_respect_switches() -> None:
    gateway = FakeGateway(FakeCommand("succeeded"))
    volume = DesktopSetVolumeTool(
        FakeResolver(), gateway, lambda: _config(allow_volume=False)
    )
    blocked = await volume.execute(
        type("Args", (), {"volume": 30, "target": None})(), _context()
    )
    assert not blocked.ok and blocked.reason_code == "desktop_action_not_allowed"

    enabled = DesktopSetVolumeTool(FakeResolver(), gateway, lambda: _config())
    ok = await enabled.execute(
        type("Args", (), {"volume": 30, "target": None})(), _context()
    )
    assert ok.ok
    assert gateway.issued[0]["command"] == "system.volume.set"
    assert gateway.issued[0]["args"] == {"volume": 30}

    clipboard = DesktopClipboardWriteTool(
        FakeResolver(), gateway, lambda: _config(allow_clipboard=False)
    )
    blocked_clip = await clipboard.execute(
        type("Args", (), {"text": "hello", "target": None})(), _context()
    )
    assert not blocked_clip.ok and blocked_clip.reason_code == "desktop_action_not_allowed"


async def test_action_guards_idempotency_user_and_offline_device() -> None:
    gateway = FakeGateway(FakeCommand("succeeded"))
    tool = DesktopOpenAppTool(FakeResolver(), gateway, lambda: _config())
    missing_key = await tool.execute(
        type("Args", (), {"app": "Safari", "target": None})(),
        _context(idempotency_key=None),
    )
    assert not missing_key.ok and missing_key.reason_code == "action_idempotency_required"

    anonymous = ToolContext(
        privacy_level=PrivacyLevel.L1, user_id=None, turn_id=None, idempotency_key="k"
    )
    missing_user = await tool.execute(
        type("Args", (), {"app": "Safari", "target": None})(), anonymous
    )
    assert not missing_user.ok and missing_user.reason_code == "action_user_required"

    offline = DesktopOpenAppTool(
        FakeResolver(runtime_error=True), gateway, lambda: _config()
    )
    unavailable = await offline.execute(
        type("Args", (), {"app": "Safari", "target": None})(), _context()
    )
    assert not unavailable.ok and unavailable.reason_code == "desktop_channel_unavailable"


async def test_failed_device_command_maps_reason() -> None:
    gateway = FakeGateway(FakeCommand("failed", reason_code="screen_locked"))
    tool = DesktopSetVolumeTool(FakeResolver(), gateway, lambda: _config())
    result = await tool.execute(
        type("Args", (), {"volume": 10, "target": None})(), _context()
    )
    assert not result.ok and result.reason_code == "screen_locked"


# ---------------------------------------------------------------------------
# Action Registry 注册与安全契约
# ---------------------------------------------------------------------------


def test_registry_registers_four_desktop_actions() -> None:
    registry = build_builtin_action_registry()
    preauthorized = (ActionRisk.A1_LOW, ConfirmationPolicy.PREAUTHORIZED)
    expected = {
        "desktop.app.open": (*preauthorized, "desktop_open_app"),
        "desktop.url.open": (*preauthorized, "desktop_open_url"),
        "system.volume.set": (*preauthorized, "desktop_set_volume"),
        "desktop.clipboard.write": (
            ActionRisk.A2_CONFIRM,
            ConfirmationPolicy.ALWAYS,
            "desktop_clipboard_write",
        ),
    }
    for action_id, (risk, policy, tool_name) in expected.items():
        compiled = registry.get(action_id)
        assert compiled is not None, action_id
        assert compiled.definition.risk == risk
        assert compiled.definition.confirmation_policy == policy
        assert compiled.definition.tool_name == tool_name
        assert compiled.definition.max_privacy_level == PrivacyLevel.L1
        assert compiled.definition.verification_policy == "receipt"


def test_desktop_actions_reject_l2_via_egress_guard() -> None:
    """L2 私密会话在执行层被拒：工具声明 max_privacy_level=L1。"""
    from app.privacy import EgressBlocked, EgressDestination, EgressGuard

    registry = build_builtin_action_registry()
    guard = EgressGuard()
    cases: list[tuple[str, dict[str, object]]] = [
        ("desktop.app.open", {"app": "Safari"}),
        ("desktop.url.open", {"url": "https://github.com/aria"}),
        ("system.volume.set", {"volume": 50}),
        ("desktop.clipboard.write", {"text": "hello"}),
    ]
    for action_id, args in cases:
        registered = registry.get(action_id)
        assert registered is not None
        compiled = registry.compile(action_id, args)
        assert compiled.tool_arguments
        with pytest.raises(EgressBlocked):
            guard.authorize(
                PrivacyLevel.L2,
                EgressDestination(
                    name=registered.definition.tool_name,
                    runs_local=True,
                    max_privacy_level=registered.definition.max_privacy_level,
                ),
            )


# ---------------------------------------------------------------------------
# 聊天挂载隔离：PC-01 动作不作为聊天工具暴露
# ---------------------------------------------------------------------------


def _hub_config() -> HubConfig:
    return HubConfig.model_validate(
        {
            "schema_version": 1,
            "models": {
                "cloud": {
                    "provider": "openai_compatible",
                    "model": "dialogue-v1",
                    "base_url": "https://models.example/v1",
                    "secret_ref": "env:MODEL_API_KEY",
                    "runs_local": False,
                    "max_privacy_level": "L1",
                    "supports_tool_calling": True,
                    "max_context_tokens": 32768,
                    "input_cost_per_million": 0,
                    "output_cost_per_million": 0,
                },
                "local": {
                    "provider": "openai_compatible",
                    "model": "local-model",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "runs_local": True,
                    "max_privacy_level": "L2",
                    "max_context_tokens": 32768,
                    "input_cost_per_million": 0,
                    "output_cost_per_million": 0,
                },
            },
            "routes": {
                "dialogue": {"primary": "cloud"},
                "utility": {"primary": "cloud"},
                "private": {"primary": "local"},
            },
            "tools": {"desktop_actions": _config().model_dump(mode="json")},
        }
    )


def test_desktop_action_tools_never_mount_to_chat() -> None:
    from app.chat.service import _device_tool_ready
    from app.llm import LLMRoute

    config = _hub_config()
    for name in (
        "desktop_open_app",
        "desktop_open_url",
        "desktop_set_volume",
        "desktop_clipboard_write",
    ):
        for privacy in (PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2):
            assert _device_tool_ready(name, config, privacy, LLMRoute.DIALOGUE) is False
