"""MAIL-01 已发送邮件日志：确认发送成功后追加，供本地回查发送历史。

与 attachments.py 的短命暂存不同，这里的行永久保留（个人单用户部署，
量级远不足以需要清理策略）；正文与附件内容不入库，只留回查所需的元数据。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select

from app.db import Database, MailOutboxLogRecord

LIST_WINDOW = 100


class MailOutboxStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def record(
        self,
        *,
        user_id: UUID,
        turn_id: UUID,
        to: list[str],
        cc: list[str],
        subject: str,
        message_id: str,
        attachments: list[str],
        sent_at: datetime | None = None,
    ) -> MailOutboxLogRecord:
        record = MailOutboxLogRecord(
            id=uuid4(),
            user_id=user_id,
            turn_id=turn_id,
            to_addresses=list(to),
            cc_addresses=list(cc),
            subject=subject,
            message_id=message_id,
            attachments=list(attachments),
            sent_at=sent_at or datetime.now(UTC),
        )
        async with self._database.sessions.begin() as session:
            session.add(record)
        return record

    async def list_recent(
        self, user_id: UUID, *, limit: int = LIST_WINDOW
    ) -> list[MailOutboxLogRecord]:
        async with self._database.sessions() as session:
            records = (
                (
                    await session.execute(
                        select(MailOutboxLogRecord)
                        .where(MailOutboxLogRecord.user_id == user_id)
                        .order_by(MailOutboxLogRecord.sent_at.desc(), MailOutboxLogRecord.id)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return list(records)
