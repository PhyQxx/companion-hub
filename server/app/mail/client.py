"""MAIL-01 邮件客户端：SMTP 发送 + IMAP 收件箱拉取/搜索。

smtplib/imaplib 是同步标准库客户端，全部经 asyncio.to_thread 调用，
不阻塞事件循环。授权码只在连接时使用，不进入日志与结果载荷。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import smtplib
from dataclasses import dataclass
from datetime import datetime
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from imaplib import IMAP4, IMAP4_SSL
from typing import Any

from app.config import ConfigStore, DatabaseConfigStore
from app.config.models import MailConfig

logger = logging.getLogger("app.mail.client")

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SNIPPET_CHARS = 200


class MailError(Exception):
    """邮箱不可用或配置缺失；reason 用 snake_code 透传给工具结果。"""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class MailAccount:
    address: str
    secret: str
    config: MailConfig


@dataclass(frozen=True, slots=True)
class MailSummary:
    uid: int
    sender: str
    subject: str
    sent_at: datetime | None
    snippet: str


def resolve_mail_account(config: MailConfig) -> MailAccount | None:
    """从配置解析账号；未启用或授权码缺失返回 None。"""
    if not config.enabled or config.address is None:
        return None
    secret = config.secret_value
    if secret is None and config.secret_ref is not None:
        import os

        secret = os.environ.get(config.secret_ref.removeprefix("env:"), None)
        if secret is None:
            logger.warning("mail secret env unavailable: %s", config.secret_ref)
            return None
    if secret is None:
        return None
    return MailAccount(address=config.address, secret=secret, config=config)


def valid_address(value: str) -> bool:
    return bool(_EMAIL_PATTERN.match(value))


def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _build_message(
    account: MailAccount,
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None,
) -> EmailMessage:
    message = EmailMessage()
    sender_name = account.config.display_name
    message["From"] = (
        formataddr((sender_name, account.address)) if sender_name else account.address
    )
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=account.address.split("@")[-1])
    message.set_content(body)
    return message


class MailClient:
    """每次操作独立连接：邮件频率低，避免维护长连接与 IMAP 状态。"""

    def __init__(self, config_store: ConfigStore | DatabaseConfigStore) -> None:
        self._config_store = config_store

    def _account(self) -> MailAccount:
        account = resolve_mail_account(self._config_store.current.config.integrations.mail)
        if account is None:
            raise MailError("mail_not_configured")
        return account

    def is_configured(self) -> bool:
        """配置就绪探测（工具挂载 available 用）；不发网络请求。"""
        try:
            self._account()
        except MailError:
            return False
        return True

    async def send(
        self,
        *,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
    ) -> dict[str, object]:
        account = self._account()
        message = _build_message(account, to=to, subject=subject, body=body, cc=cc)
        try:
            message_id = await asyncio.to_thread(self._send_sync, account, message)
        except smtplib.SMTPAuthenticationError:
            logger.warning("mail smtp auth failed for %s", account.address)
            raise MailError("mail_auth_failed") from None
        except (smtplib.SMTPException, OSError):
            logger.warning("mail smtp send failed", exc_info=True)
            raise MailError("mail_send_failed") from None
        return {"message_id": message_id, "recipients": [*to, *(cc or [])]}

    def _send_sync(self, account: MailAccount, message: EmailMessage) -> str:
        config = account.config
        if config.smtp_use_ssl:
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                config.smtp_host, config.smtp_port, timeout=config.timeout_seconds
            )
        else:
            server = smtplib.SMTP(
                config.smtp_host, config.smtp_port, timeout=config.timeout_seconds
            )
        try:
            if not config.smtp_use_ssl:
                server.starttls()
            server.login(account.address, account.secret)
            server.send_message(message)
            message_id = str(message["Message-ID"])
            return message_id.strip("<>")
        finally:
            with contextlib.suppress(Exception):
                server.quit()

    async def fetch_inbox(
        self,
        *,
        query: str | None = None,
        limit: int | None = None,
    ) -> list[MailSummary]:
        account = self._account()
        max_count = limit or account.config.fetch_limit
        try:
            summaries = await asyncio.to_thread(self._fetch_sync, account, query, max_count)
        except MailError:
            raise
        except IMAP4.error:
            logger.warning("mail imap auth failed for %s", account.address)
            raise MailError("mail_auth_failed") from None
        except (IMAP4.abort, OSError):
            logger.warning("mail imap fetch failed", exc_info=True)
            raise MailError("mail_fetch_failed") from None
        return summaries

    def _fetch_sync(
        self, account: MailAccount, query: str | None, limit: int
    ) -> list[MailSummary]:
        import email
        from email import policy

        config = account.config
        client = IMAP4_SSL(config.imap_host, config.imap_port, timeout=config.timeout_seconds)
        try:
            client.login(account.address, account.secret)
            client.select("INBOX", readonly=True)
            if query:
                status, data = client.search(None, "TEXT", _imap_quote(query))
            else:
                status, data = client.search(None, "ALL")
            if status != "OK" or not data or not data[0]:
                return []
            uids = data[0].split()[-limit:]
            summaries: list[MailSummary] = []
            for uid in reversed(uids):
                status, fetched = client.fetch(uid, "(BODY.PEEK[])")
                if status != "OK" or not fetched or not fetched[0]:
                    continue
                raw = fetched[0][1]
                if isinstance(raw, bytearray | bytes):
                    raw = bytes(raw)
                else:
                    continue
                parsed = email.message_from_bytes(raw, policy=policy.default)
                summaries.append(_to_summary(int(uid), parsed))
            return summaries
        finally:
            with contextlib.suppress(Exception):
                client.logout()


def _imap_quote(query: str) -> str:
    cleaned = query.replace("\\", " ").replace('"', " ").strip()
    return f'"{cleaned}"' if cleaned else '""'


def _to_summary(uid: int, parsed: Any) -> MailSummary:
    sender_header = parsed.get("From", "")
    sender = _decode_header_value(str(sender_header))
    name, addr = parseaddr(sender)
    subject = _decode_header_value(parsed.get("Subject", None))
    sent_at: datetime | None = None
    try:
        from email.utils import parsedate_to_datetime

        sent_at = parsedate_to_datetime(parsed.get("Date", ""))
    except Exception:
        sent_at = None
    return MailSummary(
        uid=uid,
        sender=name or addr or sender,
        subject=subject,
        sent_at=sent_at,
        snippet=_snippet(parsed),
    )


def _snippet(parsed: Any, *, max_chars: int = _SNIPPET_CHARS) -> str:
    body = ""
    try:
        if parsed.is_multipart():
            for part in parsed.walk():
                if part.get_content_type() == "text/plain" and not part.get_filename():
                    body = part.get_content()
                    break
        else:
            body = parsed.get_content()
    except Exception:
        body = ""
    if not isinstance(body, str):
        return ""
    cleaned = " ".join(body.split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars] + "…"
