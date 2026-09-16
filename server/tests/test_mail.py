"""MAIL-01 邮件助手：配置校验、客户端契约、两段式发送与只读收件。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

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

    def raising(
        account: object,
        query: str | None,
        limit: int,
        unread_only: bool = False,
        folder: str = "INBOX",
    ) -> list[object]:
        del account, query, limit, unread_only, folder
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
    draft = (await send_tool.list_drafts(context.user_id))[0]
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
        self: MailClient,
        *,
        query: str | None = None,
        limit: int | None = None,
        unread_only: bool = False,
        folder: str = "INBOX",
    ) -> list[MailSummary]:
        del self, query, limit, unread_only, folder
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
    draft = (await tool.list_drafts(context.user_id))[0]
    draft_id = UUID(str(draft["id"]))
    digest = str(draft["digest"])
    assert await tool.list_drafts(uuid4()) == []
    with pytest.raises(LookupError):
        await tool.confirm(uuid4(), draft_id, digest)
    with pytest.raises(ValueError, match="changed"):
        await tool.confirm(context.user_id, draft_id, "0" * 64)
    # 修改预览内容使旧确认失效。
    await tool.execute(args.model_copy(update={"body": "Changed body"}), context)
    with pytest.raises(ValueError, match="not_pending"):
        await tool.confirm(context.user_id, draft_id, digest)
    newer = (await tool.list_drafts(context.user_id))[-1]
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
    draft = (await tool.list_drafts(context.user_id))[0]
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
    assert (await tool.list_drafts(context.user_id))[0]["status"] == "unknown_outcome"
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
            cancelled_draft = (await tool.list_drafts(owner.principal.user_id))[-1]
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


# ---------------------------------------------------------------------------
# MAIL-01 增强：未读管理与附件发送
# ---------------------------------------------------------------------------


async def test_attachment_store_lifecycle_and_validation(database_mail: None = None) -> None:
    from app.db import Base, create_database
    from app.mail import MailAttachmentStore

    database = create_database("sqlite+aiosqlite:///:memory:")
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        auth_db = database
        from app.auth import AuthService

        auth = AuthService(auth_db)
        owner = await auth.setup(display_name="Attach", password="correct horse")
        store = MailAttachmentStore(database)
        user = owner.principal.user_id

        record = await store.create(
            user_id=user, filename="报告.pdf", mime_type="application/pdf", payload=b"%PDF-1"
        )
        assert record.filename == "报告.pdf"
        assert record.mime_type == "application/pdf"
        assert record.size_bytes == 6

        # 白名单外类型拒绝（无扩展名 + 未知声明）
        with pytest.raises(ValueError, match="unsupported_attachment_type"):
            await store.create(
                user_id=user, filename="blob.bin", mime_type="application/x-elf", payload=b"x"
            )
        # 大小上限
        with pytest.raises(ValueError, match="attachment_too_large"):
            await store.create(
                user_id=user,
                filename="big.png",
                mime_type="image/png",
                payload=b"0" * (10 * 1024 * 1024 + 1),
            )
        # 用户隔离
        other = uuid4()
        with pytest.raises(LookupError):
            await store.get_owned(other, record.id)
        # 列表/丢弃/过期清理
        assert [item.id for item in await store.list_pending(user)] == [record.id]
        await store.discard(user, record.id)
        assert await store.list_pending(user) == []
        assert await store.delete_expired() == 0  # 已丢弃的行过期前不重复删
    finally:
        await database.close()


async def test_attachment_upload_and_send_flow(
    tmp_path: Path, context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api.mail import create_mail_router
    from app.auth import AuthService
    from app.db import Base, create_database
    from app.mail import MailAttachmentStore

    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'attach.db'}")
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        auth = AuthService(database)
        owner = await auth.setup(display_name="Mail", password="correct horse")
        attachments = MailAttachmentStore(database)
        client = MailClient(await _mail_store_enabled(tmp_path))
        tool = MailSendTool(client, attachments=attachments)
        sends: list[dict[str, Any]] = []

        async def send(**kwargs: Any) -> dict[str, object]:
            sends.append(kwargs)
            return {"message_id": "with-attachment"}

        monkeypatch.setattr(client, "send", send)

        app = FastAPI()
        app.include_router(create_mail_router(tool, auth, attachments=attachments))
        headers = {"Authorization": f"Bearer {owner.access_token}"}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            uploaded = await http.post(
                "/api/v1/mail/attachments",
                headers=headers,
                files={"file": ("照片.png", io.BytesIO(b"\x89PNG-data"), "image/png")},
            )
            bad_type = await http.post(
                "/api/v1/mail/attachments",
                headers=headers,
                files={"file": ("evil.exe", io.BytesIO(b"MZ"), "application/x-msdownload")},
            )
            listed = await http.get("/api/v1/mail/attachments", headers=headers)
        assert uploaded.status_code == 201
        attachment_id = uploaded.json()["id"]
        assert uploaded.json()["mime_type"] == "image/png"
        assert bad_type.status_code == 422
        assert [item["id"] for item in listed.json()] == [attachment_id]

        # 模型引用附件准备预览 → 用户确认后附件进入 SMTP 载荷
        preview = await tool.execute(
            MailSendTool.arguments_model.model_validate(
                {
                    "to": ["friend@example.com"],
                    "subject": "带附件",
                    "body": "见附件",
                    "attachment_ids": [attachment_id],
                }
            ),
            ToolContext(
                privacy_level=PrivacyLevel.L1,
                user_id=owner.principal.user_id,
                turn_id=uuid4(),
            ),
        )
        assert preview.ok is True
        assert preview.data["preview"]["attachments"][0]["filename"] == "照片.png"
        draft = (await tool.list_drafts(owner.principal.user_id))[0]
        receipt = await tool.confirm(
            owner.principal.user_id, UUID(str(draft["id"])), str(draft["digest"])
        )
        assert receipt["status"] == "sent"
        assert len(sends) == 1
        assert sends[0]["attachments"][0].filename == "照片.png"
        assert sends[0]["attachments"][0].payload.startswith(b"\x89PNG")
        # 发送后附件被消费，不再出现在待发送列表
        assert await attachments.list_pending(owner.principal.user_id) == []
    finally:
        await database.close()


async def test_mark_tool_and_unread_summary(
    tmp_path: Path, context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from app.mail import MailMarkTool
    from app.mail.client import MailSummary

    store = await _mail_store_enabled(tmp_path)
    read_tool = MailReadTool(MailClient(store))
    mark_tool = MailMarkTool(MailClient(store))

    async def fake_fetch(
        self: MailClient,
        *,
        query: str | None = None,
        limit: int | None = None,
        unread_only: bool = False,
        folder: str = "INBOX",
    ) -> list[MailSummary]:
        del self, query, limit, folder
        assert unread_only is True  # unread_only 参数透传
        return [
            MailSummary(
                uid=7,
                sender="张三 <z@example.com>",
                subject="未读",
                sent_at=_dt(2026, 9, 3, 8, 0, tzinfo=_UTC),
                snippet="新邮件",
                unread=True,
            )
        ]

    monkeypatch.setattr(MailClient, "fetch_inbox", fake_fetch)
    result = await read_tool.execute(
        MailReadTool.arguments_model.model_validate({"unread_only": True}), context
    )
    assert result.ok is True
    assert result.data["messages"][0]["unread"] is True
    assert result.data["unread_count"] == 1

    calls: list[tuple[int, bool]] = []

    async def fake_mark(
        self: MailClient, uid: int, *, read: bool, folder: str = "INBOX"
    ) -> bool:
        del self, folder
        calls.append((uid, read))
        return True

    monkeypatch.setattr(MailClient, "mark_read", fake_mark)
    marked = await mark_tool.execute(
        MailMarkTool.arguments_model.model_validate({"uid": 7, "read": True}),
        context.model_copy(update={"privacy_level": PrivacyLevel.L1}),
    )
    assert marked.ok is True
    assert calls == [(7, True)]
    private = await mark_tool.execute(
        MailMarkTool.arguments_model.model_validate({"uid": 7}),
        context.model_copy(update={"privacy_level": PrivacyLevel.L2}),
    )
    assert private.ok is False
    assert private.reason_code == "private_session_unsupported"


def test_mime_attachment_build() -> None:
    from app.config.models import MailConfig
    from app.mail.client import MailAccount, MailAttachment, _build_message

    account = MailAccount(
        address="aria@example.com", secret="x", config=MailConfig(smtp_host="smtp.test")
    )
    message = _build_message(
        account,
        to=["b@c.com"],
        subject="附件",
        body="正文",
        cc=None,
        attachments=[
            MailAttachment(filename="照片.png", mime_type="image/png", payload=b"\x89PNG")
        ],
    )
    raw = message.as_bytes()
    assert any(
        part.get_content_type() == "image/png" and part.get_filename() == "照片.png"
        for part in message.walk()
    )
    assert b"\x89PNG" not in raw  # base64 编码后不含原始字节


# ---------------------------------------------------------------------------
# MAIL-01 文件夹归类
# ---------------------------------------------------------------------------


async def test_folder_tools_list_and_move(
    tmp_path: Path, context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.mail import MailFoldersTool, MailMoveTool

    store = await _mail_store_enabled(tmp_path)
    client = MailClient(store)
    folders_tool = MailFoldersTool(client)
    move_tool = MailMoveTool(client)

    async def fake_list(self: MailClient) -> list[str]:
        del self
        return ["INBOX", "Archive", "工作"]

    async def fake_move(self: MailClient, uid: int, *, to_folder: str, folder: str) -> bool:
        moves.append({"uid": uid, "to": to_folder, "from": folder})
        return to_folder != "NoSuch"

    moves: list[dict[str, Any]] = []
    monkeypatch.setattr(MailClient, "list_folders", fake_list)
    monkeypatch.setattr(MailClient, "move_message", fake_move)

    listed = await folders_tool.execute(
        MailFoldersTool.arguments_model.model_validate({}),
        context.model_copy(update={"privacy_level": PrivacyLevel.L1}),
    )
    assert listed.ok is True
    assert listed.data["folders"] == ["INBOX", "Archive", "工作"]

    moved = await move_tool.execute(
        MailMoveTool.arguments_model.model_validate({"uid": 7, "to_folder": "工作"}),
        context.model_copy(update={"privacy_level": PrivacyLevel.L1}),
    )
    assert moved.ok is True
    assert moves == [{"uid": 7, "to": "工作", "from": "INBOX"}]

    # 目标文件夹不存在：先经 list_folders 校验拒绝，不发起 MOVE
    missing = await move_tool.execute(
        MailMoveTool.arguments_model.model_validate({"uid": 7, "to_folder": "不存在"}),
        context.model_copy(update={"privacy_level": PrivacyLevel.L1}),
    )
    assert missing.ok is False
    assert missing.reason_code == "folder_not_found"
    assert len(moves) == 1

    private = await folders_tool.execute(
        MailFoldersTool.arguments_model.model_validate({}),
        context.model_copy(update={"privacy_level": PrivacyLevel.L2}),
    )
    assert private.ok is False


async def test_mail_read_folder_param_flows(
    tmp_path: Path, context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.mail.client import MailSummary

    store = await _mail_store_enabled(tmp_path)
    read_tool = MailReadTool(MailClient(store))
    seen_folders: list[str] = []

    async def fake_fetch(
        self: MailClient,
        *,
        query: str | None = None,
        limit: int | None = None,
        unread_only: bool = False,
        folder: str = "INBOX",
    ) -> list[MailSummary]:
        del self, query, limit, unread_only
        seen_folders.append(folder)
        return []

    monkeypatch.setattr(MailClient, "fetch_inbox", fake_fetch)
    result = await read_tool.execute(
        MailReadTool.arguments_model.model_validate({"folder": "工作"}),
        context.model_copy(update={"privacy_level": PrivacyLevel.L1}),
    )
    assert result.ok is True
    assert seen_folders == ["工作"]


async def test_mail_attachments_tool_lists_pending_for_model(
    database_mail: None = None,
) -> None:
    from app.auth import AuthService
    from app.db import Base, create_database
    from app.mail import MailAttachmentsTool, MailAttachmentStore

    database = create_database("sqlite+aiosqlite:///:memory:")
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        auth = AuthService(database)
        owner = await auth.setup(display_name="Att", password="correct horse")
        store = MailAttachmentStore(database)
        record = await store.create(
            user_id=owner.principal.user_id,
            filename="照片.png",
            mime_type="image/png",
            payload=b"\x89PNG",
        )
        tool = MailAttachmentsTool(store)
        context = ToolContext(
            privacy_level=PrivacyLevel.L1,
            user_id=owner.principal.user_id,
            turn_id=uuid4(),
        )
        result = await tool.execute(
            MailAttachmentsTool.arguments_model.model_validate({}), context
        )
        assert result.ok is True
        assert result.data["count"] == 1
        assert result.data["attachments"][0]["id"] == str(record.id)
        assert result.data["attachments"][0]["filename"] == "照片.png"

        private = await tool.execute(
            MailAttachmentsTool.arguments_model.model_validate({}),
            context.model_copy(update={"privacy_level": PrivacyLevel.L2}),
        )
        assert private.ok is False
        assert private.reason_code == "private_session_unsupported"
    finally:
        await database.close()
