"""WEB-01 浏览器工作流：配置开关、工具门禁、动作注册与聊天挂载隔离。"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError

from app.cognition.action_registry import build_builtin_action_registry
from app.config import BrowserWorkflowConfig, HubConfig
from app.schemas.common import PrivacyLevel
from app.tools.browser_form import (
    BrowserFormFillTool,
    BrowserFormReadArgs,
    BrowserFormReadTool,
    BrowserFormSubmitTool,
    BrowserOpenTabTool,
)
from app.tools.contracts import ToolContext

SNAPSHOT_ID = "a" * 32

DEVICE_ID = UUID("00000000-0000-0000-0000-00000000bef1")


def _config(**overrides: Any) -> BrowserWorkflowConfig:
    values: dict[str, Any] = {"enabled": True}
    values.update(overrides)
    return BrowserWorkflowConfig.model_validate(values)


class FakeResolver:
    def __init__(self, *, runtime_error: bool = False) -> None:
        self.runtime_error = runtime_error
        self.requested: list[dict[str, Any]] = []

    async def resolve(
        self, *, owner_user_id: UUID, target: str | UUID | None, capability: str
    ) -> Any:
        self.requested.append({"capability": capability, "target": target})
        if self.runtime_error:
            raise RuntimeError("no device")
        return type("Device", (), {"id": DEVICE_ID})()


class FakeCommand:
    def __init__(self, status: str, reason_code: str | None = None) -> None:
        self.id = uuid4()
        self.status = status
        self.reason_code = reason_code
        self.result_meta: dict[str, Any] | None = None


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
        self.issued.append({"command": command, "args": args})
        return FakeCommand("succeeded")

    async def wait_for_terminal(
        self,
        command_id: UUID,
        *,
        timeout_seconds: float | None = None,
    ) -> FakeCommand:
        return self.terminal


def _context(*, idempotency_key: str | None = "plan-step-1") -> ToolContext:
    return ToolContext(
        privacy_level=PrivacyLevel.L1,
        user_id=UUID("00000000-0000-0000-0000-00000000bef2"),
        turn_id=None,
        idempotency_key=idempotency_key,
    )


def _args(model: type[BaseModel], **values: Any) -> Any:
    return model.model_validate(values)


# ---------------------------------------------------------------------------
# 工具门禁
# ---------------------------------------------------------------------------


async def test_all_tools_require_master_switch() -> None:
    gateway = FakeGateway(FakeCommand("succeeded"))
    cases: list[tuple[Any, dict[str, Any], str, str]] = [
        (
            BrowserOpenTabTool(FakeResolver(), gateway, lambda: _config(enabled=False)),
            {"url": "https://example.com"},
            "browser.tab.open",
            "browser_workflow_not_allowed",
        ),
        (
            BrowserFormReadTool(FakeResolver(), gateway, lambda: _config(enabled=False)),
            {},
            "browser.form.read",
            "browser_workflow_not_allowed",
        ),
        (
            BrowserFormFillTool(FakeResolver(), gateway, lambda: _config(enabled=False)),
            {"snapshot_id": SNAPSHOT_ID, "fields": [{"ref": "f0", "value": "x"}]},
            "browser.form.fill",
            "browser_workflow_not_allowed",
        ),
        (
            BrowserFormSubmitTool(FakeResolver(), gateway, lambda: _config(enabled=False)),
            {"snapshot_id": SNAPSHOT_ID, "form_ref": "form0"},
            "browser.form.submit",
            "browser_workflow_not_allowed",
        ),
    ]
    for tool, values, _command, reason in cases:
        result = await tool.execute(_args(tool.arguments_model, **values), _context())
        assert not result.ok and result.reason_code == reason
    assert gateway.issued == []


async def test_open_tab_rejects_non_http_schemes() -> None:
    gateway = FakeGateway(FakeCommand("succeeded"))
    tool = BrowserOpenTabTool(FakeResolver(), gateway, lambda: _config())
    for url in ("javascript:alert(1)", "ftp://example.com", "not a url", "https://"):
        result = await tool.execute(_args(tool.arguments_model, url=url), _context())
        assert not result.ok and result.reason_code == "invalid_target_url"
    assert gateway.issued == []
    ok = await tool.execute(_args(tool.arguments_model, url="https://GitHub.com/aria"), _context())
    assert ok.ok
    assert gateway.issued[0]["command"] == "browser.tab.open"


async def test_form_read_and_fill_dispatch_with_capability_and_bounded_args() -> None:
    gateway = FakeGateway(FakeCommand("succeeded"))
    read = await BrowserFormReadTool(FakeResolver(), gateway, lambda: _config()).execute(
        _args(BrowserFormReadArgs), _context()
    )
    assert read.ok
    assert gateway.issued[-1]["command"] == "browser.form.read"
    assert gateway.issued[-1]["args"] == {}

    fill = BrowserFormFillTool(FakeResolver(), gateway, lambda: _config())
    result = await fill.execute(
        _args(
            fill.arguments_model,
            snapshot_id=SNAPSHOT_ID,
            fields=[{"ref": "f0", "value": "aria"}, {"ref": "f3", "value": "你好"}],
        ),
        _context(),
    )
    assert result.ok
    assert gateway.issued[-1]["command"] == "browser.form.fill"
    assert gateway.issued[-1]["args"] == {
        "snapshot_id": SNAPSHOT_ID,
        "fields": [{"ref": "f0", "value": "aria"}, {"ref": "f3", "value": "你好"}],
    }


async def test_fill_rejects_malformed_field_plan() -> None:
    gateway = FakeGateway(FakeCommand("succeeded"))
    tool = BrowserFormFillTool(FakeResolver(), gateway, lambda: _config())
    for fields in ([], [{"ref": "field-1", "value": "x"}], [{"ref": "f0", "value": ""}]):
        with pytest.raises(ValidationError):
            _args(tool.arguments_model, snapshot_id=SNAPSHOT_ID, fields=fields)
    assert gateway.issued == []


async def test_submit_is_a_device_receipt_only_after_dispatch() -> None:
    gateway = FakeGateway(FakeCommand("succeeded"))
    tool = BrowserFormSubmitTool(FakeResolver(), gateway, lambda: _config())
    result = await tool.execute(
        _args(tool.arguments_model, snapshot_id=SNAPSHOT_ID, form_ref="form2"), _context()
    )
    assert result.ok
    assert gateway.issued[-1] == {
        "command": "browser.form.submit",
        "args": {"snapshot_id": SNAPSHOT_ID, "form_ref": "form2"},
    }


async def test_failure_receipt_and_guards_surface_reasons() -> None:
    offline = BrowserFormReadTool(
        FakeResolver(runtime_error=True), FakeGateway(FakeCommand("succeeded")), lambda: _config()
    )
    unavailable = await offline.execute(_args(BrowserFormReadArgs), _context())
    assert not unavailable.ok and unavailable.reason_code == "browser_channel_unavailable"

    failing = BrowserOpenTabTool(
        FakeResolver(),
        FakeGateway(FakeCommand("failed", reason_code="restricted_page")),
        lambda: _config(),
    )
    result = await failing.execute(
        _args(failing.arguments_model, url="https://example.com"),
        _context(),
    )
    assert not result.ok and result.reason_code == "restricted_page"

    missing_key = BrowserFormReadTool(
        FakeResolver(), FakeGateway(FakeCommand("succeeded")), lambda: _config()
    )
    rejected = await missing_key.execute(_args(BrowserFormReadArgs), _context(idempotency_key=None))
    assert not rejected.ok and rejected.reason_code == "action_idempotency_required"


# ---------------------------------------------------------------------------
# Action Registry 契约
# ---------------------------------------------------------------------------


def test_registry_registers_browser_workflow_actions() -> None:
    registry = build_builtin_action_registry()
    expected = {
        "browser.tab.open": ("A1", "preauthorized", "browser_open_tab"),
        "browser.form.read": ("A0", "never", "browser_form_read"),
        "browser.form.fill": ("A1", "preauthorized", "browser_form_fill"),
        "browser.form.submit": ("A2", "always", "browser_form_submit"),
    }
    for action_id, (risk, policy, tool_name) in expected.items():
        registered = registry.get(action_id)
        assert registered is not None, action_id
        assert registered.definition.risk == risk
        assert registered.definition.confirmation_policy == policy
        assert registered.definition.tool_name == tool_name
        assert registered.definition.max_privacy_level == PrivacyLevel.L1
        assert registered.definition.verification_policy == "receipt"


def test_browser_workflow_capabilities_required_for_mounting() -> None:
    """挂载层面：四个工具一律不出现在聊天工具里（计划—确认—执行专用）。"""
    from app.chat.service import _device_tool_ready
    from app.llm import LLMRoute

    config = HubConfig.model_validate(
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
            "tools": {"browser_workflow": {"enabled": True}},
        }
    )
    for name in (
        "browser_open_tab",
        "browser_form_read",
        "browser_form_fill",
        "browser_form_submit",
    ):
        for privacy in (PrivacyLevel.L0, PrivacyLevel.L1, PrivacyLevel.L2):
            assert _device_tool_ready(name, config, privacy, LLMRoute.DIALOGUE) is False


def test_form_snapshot_required_and_bounded() -> None:
    from app.tools.browser_form import BrowserFormFillArgs, BrowserFormSubmitArgs

    for model, params in (
        (BrowserFormFillArgs, {"fields": [{"ref": "f0", "value": "x"}]}),
        (BrowserFormSubmitArgs, {"form_ref": "form0"}),
    ):
        for invalid in (None, "", "guess", "a" * 33):
            with pytest.raises(ValidationError):
                model.model_validate({**params, "snapshot_id": invalid})
        with pytest.raises(ValidationError):
            model.model_validate(params)
        assert (
            model.model_validate({**params, "snapshot_id": SNAPSHOT_ID}).snapshot_id == SNAPSHOT_ID
        )


@pytest.mark.parametrize(
    "reason",
    [
        "form_snapshot_missing",
        "form_snapshot_changed",
        "form_snapshot_expired",
        "control_not_found",
    ],
)
async def test_snapshot_failure_propagates(reason: str) -> None:
    gateway = FakeGateway(FakeCommand("failed", reason))
    tool = BrowserFormSubmitTool(FakeResolver(), gateway, lambda: _config())
    result = await tool.execute(
        _args(tool.arguments_model, snapshot_id=SNAPSHOT_ID, form_ref="form0"), _context()
    )
    assert not result.ok and result.reason_code == reason


async def test_read_result_exposes_bounded_snapshot_contract() -> None:
    terminal = FakeCommand("succeeded")
    terminal.result_meta = {
        "snapshot_id": SNAPSHOT_ID,
        "fields": [{"ref": "f0", "value": ""}],
        "truncated": False,
        "unrelated_internal": "must-not-expose",
    }
    tool = BrowserFormReadTool(FakeResolver(), FakeGateway(terminal), lambda: _config())
    result = await tool.execute(_args(BrowserFormReadArgs), _context())
    assert result.data["result"] == {
        "snapshot_id": SNAPSHOT_ID,
        "fields": [{"ref": "f0", "value": ""}],
        "truncated": False,
    }
