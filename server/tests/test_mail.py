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
    return f"    enabled: true\n    address: {ADDRESS}\n    secret_value: {SECRET}\n"


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
        MailConfig(enabled=True, address=ADDRESS, secret_ref="env:ARIA_TEST_MAIL_SECRET")
    )
    assert via_env is not None

    monkeypatch.delenv("ARIA_TEST_MAIL_SECRET")
    assert (
        resolve_mail_account(
            MailConfig(enabled=True, address=ADDRESS, secret_ref="env:ARIA_TEST_MAIL_SECRET")
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
    assert confirmed.ok is False
    assert confirmed.reason_code == "user_confirmation_required"
    assert sends == []
    assert context.user_id is not None
    draft = send_tool.list_drafts(context.user_id)[0]
    receipt = await send_tool.confirm(context.user_id, UUID(str(draft["id"])), str(draft["digest"]))
    assert receipt["status"] == "sent"
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
    assert duplicate.ok is False
    repeated = await send_tool.confirm(
        context.user_id, UUID(str(draft["id"])), str(draft["digest"])
    )
    assert repeated == receipt
    assert len(sends) == 1


async def test_send_tool_rejects_bad_address_and_l2(tmp_path: Path, context: ToolContext) -> None:
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


async def test_mail_confirmation_boundaries(
    tmp_path: Path,
    context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    now = datetime(2026, 9, 8, tzinfo=UTC)
    client = MailClient(await _mail_store_enabled(tmp_path))
    tool = MailSendTool(client, clock=lambda: now)
    sends: list[dict[str, Any]] = []

    async def send(**kwargs: Any) -> dict[str, object]:
        sends.append(kwargs)
        return {"message_id": "test"}

    monkeypatch.setattr(client, "send", send)
    args = MailSendTool.arguments_model.model_validate(
        {"to": ["friend@example.com"], "subject": "Original", "body": "Full body"}
    )
    direct = await tool.execute(args.model_copy(update={"confirmed": True}), context)
    assert direct.reason_code == "user_confirmation_required"
    assert sends == []
    assert context.user_id is not None
    for level in (PrivacyLevel.L0, PrivacyLevel.L2):
        assert not (
            await tool.execute(args, context.model_copy(update={"privacy_level": level}))
        ).ok
    preview = await tool.execute(args, context)
    replay = await tool.execute(args, context)
    assert replay.data["draft_id"] == preview.data["draft_id"]
    draft = tool.list_drafts(context.user_id)[0]
    draft_id = UUID(str(draft["id"]))
    digest = str(draft["digest"])
    assert tool.list_drafts(uuid4()) == []
    with pytest.raises(LookupError):
        await tool.confirm(uuid4(), draft_id, digest)
    with pytest.raises(ValueError, match="changed"):
        await tool.confirm(context.user_id, draft_id, "0" * 64)
    # 修改预览内容使旧确认失效。
    await tool.execute(args.model_copy(update={"body": "Changed body"}), context)
    with pytest.raises(ValueError, match="not_pending"):
        await tool.confirm(context.user_id, draft_id, digest)
    newer = tool.list_drafts(context.user_id)[-1]
    newer_id = UUID(str(newer["id"]))
    # 重启后的实例不认识旧预览，不会凭旧确认重发。
    with pytest.raises(LookupError):
        await MailSendTool(client).confirm(context.user_id, newer_id, str(newer["digest"]))
    now += timedelta(minutes=16)
    with pytest.raises(ValueError, match="expired"):
        await tool.confirm(context.user_id, newer_id, str(newer["digest"]))
    assert sends == []


async def test_mail_concurrent_confirmation_and_unknown_outcome(
    tmp_path: Path,
    context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    client = MailClient(await _mail_store_enabled(tmp_path))
    tool = MailSendTool(client)
    entered, release = asyncio.Event(), asyncio.Event()
    sends = 0

    async def send(**kwargs: Any) -> dict[str, object]:
        nonlocal sends
        sends += 1
        entered.set()
        await release.wait()
        raise MailError("mail_connection_failed")

    monkeypatch.setattr(client, "send", send)
    await tool.execute(
        MailSendTool.arguments_model.model_validate(
            {"to": ["friend@example.com"], "subject": "s", "body": "b"}
        ),
        context,
    )
    assert context.user_id is not None
    draft = tool.list_drafts(context.user_id)[0]
    draft_id, digest = UUID(str(draft["id"])), str(draft["digest"])
    first = asyncio.create_task(tool.confirm(context.user_id, draft_id, digest))
    await entered.wait()
    with pytest.raises(ValueError, match="not_pending"):
        await tool.confirm(context.user_id, draft_id, digest)
    release.set()
    with pytest.raises(MailError):
        await first
    with pytest.raises(ValueError, match="not_pending"):
        await tool.confirm(context.user_id, draft_id, digest)
    assert tool.list_drafts(context.user_id)[0]["status"] == "unknown_outcome"
    assert sends == 1


async def test_mail_confirmation_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api.mail import create_mail_router
    from app.auth import AuthService
    from app.db import Base, create_database
    from app.ids import uuid7

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'mail.db'}")
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        auth = AuthService(database)
        owner = await auth.setup(display_name="Mail user", password="correct horse")
        client = MailClient(await _mail_store_enabled(tmp_path))
        tool = MailSendTool(client)
        sent: list[dict[str, Any]] = []

        async def send(**kwargs: Any) -> dict[str, object]:
            sent.append(kwargs)
            return {"message_id": "api-test"}

        monkeypatch.setattr(client, "send", send)
        await tool.execute(
            MailSendTool.arguments_model.model_validate(
                {"to": ["friend@example.com"], "subject": "s", "body": "Exact body"}
            ),
            ToolContext(
                privacy_level=PrivacyLevel.L1, user_id=owner.principal.user_id, turn_id=uuid7()
            ),
        )
        app = FastAPI()
        app.include_router(create_mail_router(tool, auth))
        headers = {"Authorization": f"Bearer {owner.access_token}"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            assert (await http.get("/api/v1/mail/drafts")).status_code == 401
            drafts = (await http.get("/api/v1/mail/drafts", headers=headers)).json()
            draft = drafts[0]
            url = f"/api/v1/mail/drafts/{draft['id']}/confirm"
            assert (await http.post(url, json={"digest": draft["digest"]})).status_code == 401
            assert (
                await http.post(
                    url, headers=headers, json={"digest": draft["digest"], "body": "tampered"}
                )
            ).status_code == 422
            assert (
                await http.post(url, headers=headers, json={"digest": "0" * 64})
            ).status_code == 409
            assert sent == []
            for _ in range(2):
                response = await http.post(url, headers=headers, json={"digest": draft["digest"]})
                assert response.status_code == 200
                assert response.json()["status"] == "sent"
            assert len(sent) == 1
            assert sent[0]["body"] == "Exact body"
            cancelled_preview = await tool.execute(
                MailSendTool.arguments_model.model_validate(
                    {"to": ["friend@example.com"], "subject": "Cancel me", "body": "b"}
                ),
                ToolContext(
                    privacy_level=PrivacyLevel.L1, user_id=owner.principal.user_id, turn_id=uuid7()
                ),
            )
            cancelled_id = cancelled_preview.data["draft_id"]
            cancelled_draft = tool.list_drafts(owner.principal.user_id)[-1]
            cancel_url = f"/api/v1/mail/drafts/{cancelled_id}/cancel"
            assert (await http.post(cancel_url, headers=headers)).json()["status"] == "cancelled"
            assert (
                await http.post(
                    f"/api/v1/mail/drafts/{cancelled_id}/confirm",
                    headers=headers,
                    json={"digest": cancelled_draft["digest"]},
                )
            ).status_code == 409
            assert len(sent) == 1
    finally:
        await database.close()
