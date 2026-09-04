"""MAIL-01 真实联调脚本：QQ 邮箱 SMTP 发送 + IMAP 收取全链验证。

用法（凭据只走环境变量，不进仓库）：
    cd server && \
    ARIA_MAIL_ADDRESS=you@qq.com ARIA_MAIL_SECRET=授权码 \
    ../.venv/bin/python scripts/mail_livecheck.py

覆盖：SMTP 登录与自发自收、Message-ID 回执、IMAP 登录与关键词搜索、
摘要解析（发件人/主题/时间/正文片段）。
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from types import SimpleNamespace

from app.config.models import MailConfig
from app.mail import MailClient


class StaticConfigStore:
    def __init__(self, config: MailConfig) -> None:
        self._config = config

    @property
    def current(self) -> SimpleNamespace:
        mail = self._config
        integrations = SimpleNamespace(mail=mail)
        return SimpleNamespace(config=SimpleNamespace(integrations=integrations))


async def main() -> None:
    import os

    address = os.environ.get("ARIA_MAIL_ADDRESS")
    secret = os.environ.get("ARIA_MAIL_SECRET")
    if not address or not secret:
        print("缺少 ARIA_MAIL_ADDRESS / ARIA_MAIL_SECRET 环境变量，退出。")
        sys.exit(2)

    config = MailConfig(
        enabled=True,
        address=address,
        secret_value=secret,
    )
    client = MailClient(StaticConfigStore(config))  # type: ignore[arg-type]

    token = secrets.token_hex(4)
    subject = f"Aria 联调测试邮件 {token}（可删除）"

    print("== 1. SMTP 自发自收")
    receipt = await client.send(
        to=[address],
        subject=subject,
        body=f"这是 Aria MAIL-01 联调测试邮件，验证后可删除。标识：{token}",
    )
    print(f"   message_id: {receipt['message_id']}")

    print("== 2. IMAP 搜索该邮件（服务端索引可能延迟，重试 3 次）")
    found = None
    for attempt in range(1, 4):
        await asyncio.sleep(5)
        summaries = await client.fetch_inbox(query=token, limit=5)
        matches = [item for item in summaries if token in item.subject]
        print(f"   第 {attempt} 次搜索: {len(summaries)} 条结果，主题匹配 {len(matches)} 条")
        if matches:
            found = matches[0]
            break
    if found is None:
        print("!! 未在收件箱搜到测试邮件（SMTP 已成功；可能是 IMAP 索引延迟或投递过滤）")
        sys.exit(1)
    print(f"   发件人: {found.sender}")
    print(f"   主题: {found.subject}")
    print(f"   时间: {found.sent_at}")
    print(f"   摘要: {found.snippet[:80]}")
    print("== 联调完成：SMTP 发送与 IMAP 读取全链通过")


if __name__ == "__main__":
    asyncio.run(main())
