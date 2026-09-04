"""MAIL-01 邮件助手：配置校验、客户端契约、两段式发送与只读收件。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from app.config import ConfigStore, HubConfig
from app.config.models import MailConfig
from app.mail import MailClient, MailError, MailReadTool, MailSendTool, create_mail_tools
from app.mail.client import resolve_mail_account, valid_address
from app.schemas.common import PrivacyLevel
from app.tools.contracts import ToolContext

ADDRESS = "aria@example.com"
SECRET = "dummy-authorization-code"

YAML_TEMPLATE = """
schema_version: 1
models:
  cloud:
    provider: openai_compatible
    model: dialogue-v1
    base_url: https://models.example/v1
    secret_ref: env:MODEL_API_KEY
    runs_local: false
    max_privacy_level: L1
    max_context_tokens: 32768
    input_cost_per_million: 0
    output_cost_per_million: 0
  local:
    provider: openai_compatible
    model: local-model
    base_url: http://127.0.0.1:11434/v1
    runs_local: true
    max_privacy_level: L2
    max_context_tokens: 32768
    input_cost_per_million: 0
    output_cost_per_million: 0
routes:
  dialogue: {primary: cloud}
  utility: {primary: cloud}
  private: {primary: local}
"""


async def _mail_store(
    tmp_path: Path, mail_block: str | None = None, *, name: str = "hub"
) -> ConfigStore:
    body = YAML_TEMPLATE
    if mail_block is not None:
        body += "integrations:\n  mail:\n" + mail_block
    path = tmp_path / f"{name}.yaml"
    path.write_text(body, encoding="utf-8")
    store = ConfigStore(path)
    await store.load()
    return store


def _enabled_block() -> str:
    return (
        "    enabled: true\n"
        f"    address: {ADDRESS}\n"
        f"    secret_value: {SECRET}\n"
    )


@pytest.fixture
def context() -> ToolContext:
    return ToolContext(
        privacy_level=PrivacyLevel.L1,
        user_id=UUID("0198b2f4-3b00-7001-8000-0000000000aa"),
        turn_id=UUID("0198b2f4-3b00-7002-8000-0000000000bb"),
    )


# ---------------------------------------------------------------------------
# 配置与账号解析
# ---------------------------------------------------------------------------


def test_mail_config_requires_account_when_enabled() -> None:
    with pytest.raises(ValueError, match="mail requires"):
        MailConfig(enabled=True)
    MailConfig(enabled=False)  # 未启用允许留空
    assert resolve_mail_account(MailConfig()) is None


def test_resolve_mail_account_secret_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    config = MailConfig(
        enabled=True,
        address=ADDRESS,
        secret_value=SECRET,
    )
    account = resolve_mail_account(config)
    assert account is not None and account.secret == SECRET

    monkeypatch.setenv("ARIA_TEST_MAIL_SECRET", SECRET)
    via_env = resolve_mail_account(
        MailConfig(
            enabled=True, address=ADDRESS, secret_ref="env:ARIA_TEST_MAIL_SECRET"
        )
    )
    assert via_env is not None

    monkeypatch.delenv("ARIA_TEST_MAIL_SECRET")
    assert (
        resolve_mail_account(
            MailConfig(
                enabled=True, address=ADDRESS, secret_ref="env:ARIA_TEST_MAIL_SECRET"
            )
        )
        is None
    )


async def test_mail_tools_available_only_when_configured(tmp_path: Path) -> None:
    disabled_store = await _mail_store(tmp_path)
    disabled = create_mail_tools(disabled_store)
    assert all(tool.available is False for tool in disabled)

    enabled_store = await _mail_store(tmp_path, _enabled_block(), name="enabled")
    enabled = create_mail_tools(enabled_store)
    assert all(tool.available is True for tool in enabled)


async def _mail_store_enabled(tmp_path: Path) -> ConfigStore:
    return await _mail_store(tmp_path, _enabled_block(), name="enabled")


# ---------------------------------------------------------------------------
# 客户端契约（monkeypatch 同步实现，不发真实网络）
# ---------------------------------------------------------------------------


class FakeSmtpAccountCalls:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []


async def test_client_send_builds_message_and_returns_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await _mail_store_enabled(tmp_path)
    client = MailClient(store)
    from email.message import EmailMessage

    calls: list[EmailMessage] = []

    def fake_send_sync(account: object, message: EmailMessage) -> str:
        del account
        calls.append(message)
        return "id-123@example.com"

    monkeypatch.setattr(client, "_send_sync", fake_send_sync)
    receipt = await client.send(
        to=["friend@example.com"], subject="你好", body="正文", cc=["boss@example.com"]
    )

    assert receipt["message_id"] == "id-123@example.com"
    assert receipt["recipients"] == ["friend@example.com", "boss@example.com"]
    message = calls[0]
    assert message["To"] == "friend@example.com"
    assert message["Cc"] == "boss@example.com"
    assert message["Subject"] == "你好"


async def test_client_send_maps_auth_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import smtplib

    store = await _mail_store_enabled(tmp_path)
    client = MailClient(store)

    def raising(account: object, message: object) -> str:
        del account, message
        raise smtplib.SMTPAuthenticationError(535, b"auth failed")

    monkeypatch.setattr(client, "_send_sync", raising)
    with pytest.raises(MailError) as error:
        await client.send(to=["x@example.com"], subject="s", body="b")
    assert error.value.reason_code == "mail_auth_failed"


async def test_client_fetch_maps_imap_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from imaplib import IMAP4

    store = await _mail_store_enabled(tmp_path)
    client = MailClient(store)

    def raising(account: object, query: str | None, limit: int) -> list[object]:
        del account, query, limit
        raise IMAP4.error("auth failed")

    monkeypatch.setattr(client, "_fetch_sync", raising)
    with pytest.raises(MailError) as error:
        await client.fetch_inbox()
    assert error.value.reason_code == "mail_auth_failed"


# ---------------------------------------------------------------------------
# mail_send 两段式确认
# ---------------------------------------------------------------------------


async def test_send_tool_requires_confirmation(
    tmp_path: Path, context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await _mail_store_enabled(tmp_path)
    send_tool = MailSendTool(MailClient(store))
    sends: list[dict[str, Any]] = []

    async def fake_send(self: MailClient, **kwargs: Any) -> dict[str, object]:
        del self
        sends.append(kwargs)
        return {"message_id": "mid-1", "recipients": kwargs["to"]}

    monkeypatch.setattr(MailClient, "send", fake_send)

    preview = await send_tool.execute(
        MailSendTool.arguments_model.model_validate(
            {"to": ["friend@example.com"], "subject": "晚饭", "body": "今晚吃什么"}
        ),
        context,
    )
    assert preview.ok is True
    assert preview.data["sent"] is False
    assert preview.data["confirmation_required"] is True
    assert preview.data["preview"]["to"] == ["friend@example.com"]
    assert preview.data["preview"]["subject"] == "晚饭"
    assert sends == []

    confirmed = await send_tool.execute(
        MailSendTool.arguments_model.model_validate(
            {
                "to": ["friend@example.com"],
                "subject": "晚饭",
                "body": "今晚吃什么",
                "confirmed": True,
            }
        ),
        context,
    )
    assert confirmed.data["sent"] is True
    assert confirmed.data["message_id"] == "mid-1"
    assert len(sends) == 1

    duplicate = await send_tool.execute(
        MailSendTool.arguments_model.model_validate(
            {
                "to": ["friend@example.com"],
                "subject": "晚饭",
                "body": "今晚吃什么",
                "confirmed": True,
            }
        ),
        context,
    )
    assert duplicate.data["duplicate"] is True
    assert duplicate.data["message_id"] == "mid-1"
    assert len(sends) == 1


async def test_send_tool_rejects_bad_address_and_l2(
    tmp_path: Path, context: ToolContext
) -> None:
    store = await _mail_store_enabled(tmp_path)
    send_tool = MailSendTool(MailClient(store))

    invalid = await send_tool.execute(
        MailSendTool.arguments_model.model_validate(
            {"to": ["not-an-email"], "subject": "s", "body": "b", "confirmed": True}
        ),
        context,
    )
    assert invalid.ok is False
    assert invalid.reason_code == "invalid_recipient"

    private = await send_tool.execute(
        MailSendTool.arguments_model.model_validate(
            {"to": ["a@b.com"], "subject": "s", "body": "b", "confirmed": True}
        ),
        context.model_copy(update={"privacy_level": PrivacyLevel.L2}),
    )
    assert private.ok is False
    assert private.reason_code == "private_session_unsupported"


# ---------------------------------------------------------------------------
# mail_read 只读摘要
# ---------------------------------------------------------------------------


async def test_read_tool_returns_summaries_and_blocks_l2(
    tmp_path: Path, context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from app.mail.client import MailSummary

    store = await _mail_store_enabled(tmp_path)
    read_tool = MailReadTool(MailClient(store))

    async def fake_fetch(
        self: MailClient, *, query: str | None = None, limit: int | None = None
    ) -> list[MailSummary]:
        del self, query, limit
        return [
            MailSummary(
                uid=42,
                sender="张三 <zhang@example.com>",
                subject="周报",
                sent_at=datetime(2026, 9, 3, 8, 0, tzinfo=UTC),
                snippet="本周完成三件事",
            )
        ]

    monkeypatch.setattr(MailClient, "fetch_inbox", fake_fetch)
    result = await read_tool.execute(
        MailReadTool.arguments_model.model_validate({"limit": 5}), context
    )
    assert result.ok is True
    message = result.data["messages"][0]
    assert message["uid"] == 42
    assert message["subject"] == "周报"
    assert result.data["count"] == 1

    private = await read_tool.execute(
        MailReadTool.arguments_model.model_validate({}),
        context.model_copy(update={"privacy_level": PrivacyLevel.L2}),
    )
    assert private.ok is False
    assert private.reason_code == "private_session_unsupported"


def test_valid_address_patterns() -> None:
    assert valid_address("user@qq.com") is True
    assert valid_address("user.name+tag@sub.example.com") is True
    assert valid_address("bad@@example") is False
    assert valid_address("no-at-sign.example.com") is False


async def test_hub_config_accepts_mail_section(tmp_path: Path) -> None:
    store = await _mail_store_enabled(tmp_path)
    config: HubConfig = store.current.config
    assert config.integrations.mail.enabled is True
    assert config.integrations.mail.address == ADDRESS
