"""MAIL-01 待发送附件存储：上传、引用、消费与过期清理。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult

from app.db import Database, MailAttachmentRecord

ATTACHMENT_TTL_MINUTES = 60
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS_PER_USER = 12
ALLOWED_MIME_PREFIXES = (
    "image/",
    "application/pdf",
    "application/zip",
    "text/plain",
    "text/markdown",
    "application/msword",
    "application/vnd.openxmlformats-officedocument",
    "application/vnd.ms-excel",
)
# 扩展名兜底（浏览器给不出发型时按后缀映射）
_EXT_MIME = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".zip": "application/zip",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def resolve_mime(filename: str, declared: str | None) -> str:
    """只接受白名单 MIME；声明缺失或可疑时按扩展名兜底，仍不命中则拒绝。"""
    lowered = filename.lower()
    for ext, mime in _EXT_MIME.items():
        if lowered.endswith(ext):
            return mime
    if declared:
        declared = declared.split(";")[0].strip().lower()
        if any(declared.startswith(prefix) for prefix in ALLOWED_MIME_PREFIXES):
            return declared
    raise ValueError("unsupported_attachment_type")


class MailAttachmentStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(
        self,
        *,
        user_id: UUID,
        filename: str,
        mime_type: str,
        payload: bytes,
        now: datetime | None = None,
    ) -> MailAttachmentRecord:
        if not filename or len(filename) > 255:
            raise ValueError("invalid_attachment_filename")
        # 文件名只保留展示所需字符，防止头部注入
        cleaned = filename.replace("\r", " ").replace("\n", " ").replace('"', "'").strip()
        if not cleaned:
            raise ValueError("invalid_attachment_filename")
        if len(payload) > MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment_too_large")
        resolved = resolve_mime(cleaned, mime_type)
        current = now or datetime.now(UTC)
        await self.delete_expired(now=current)
        async with self._database.sessions() as session:
            count = int(
                await session.scalar(
                    select(sa.func.count())
                    .select_from(MailAttachmentRecord)
                    .where(
                        MailAttachmentRecord.user_id == user_id,
                        MailAttachmentRecord.status == "pending",
                    )
                )
                or 0
            )
        if count >= MAX_ATTACHMENTS_PER_USER:
            raise ValueError("attachment_capacity")
        record = MailAttachmentRecord(
            id=uuid4(),
            user_id=user_id,
            filename=cleaned,
            mime_type=resolved,
            size_bytes=len(payload),
            payload=payload,
            status="pending",
            expires_at=current + timedelta(minutes=ATTACHMENT_TTL_MINUTES),
            created_at=current,
        )
        async with self._database.sessions.begin() as session:
            session.add(record)
        return record

    async def get_owned(self, user_id: UUID, attachment_id: UUID) -> MailAttachmentRecord:
        async with self._database.sessions() as session:
            record = await session.scalar(
                select(MailAttachmentRecord).where(MailAttachmentRecord.id == attachment_id)
            )
        if record is None or record.user_id != user_id:
            raise LookupError("attachment_not_found")
        expires_at = record.expires_at
        if expires_at.tzinfo is None:  # SQLite 返回 naive
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            raise ValueError("attachment_expired")
        return record

    async def list_pending(self, user_id: UUID) -> list[MailAttachmentRecord]:
        await self.delete_expired()
        async with self._database.sessions() as session:
            records = (
                (
                    await session.execute(
                        select(MailAttachmentRecord)
                        .where(
                            MailAttachmentRecord.user_id == user_id,
                            MailAttachmentRecord.status == "pending",
                        )
                        .order_by(MailAttachmentRecord.created_at)
                    )
                )
                .scalars()
                .all()
            )
        return list(records)

    async def discard(self, user_id: UUID, attachment_id: UUID) -> None:
        async with self._database.sessions.begin() as session:
            await session.execute(
                update(MailAttachmentRecord)
                .where(
                    MailAttachmentRecord.id == attachment_id,
                    MailAttachmentRecord.user_id == user_id,
                    MailAttachmentRecord.status == "pending",
                )
                .values(status="discarded")
            )

    async def mark_used(self, attachment_ids: list[UUID]) -> None:
        if not attachment_ids:
            return
        async with self._database.sessions.begin() as session:
            await session.execute(
                update(MailAttachmentRecord)
                .where(MailAttachmentRecord.id.in_(attachment_ids))
                .values(status="used")
            )

    async def delete_expired(self, *, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                delete(MailAttachmentRecord).where(
                    MailAttachmentRecord.expires_at <= current,
                    MailAttachmentRecord.status != "used",
                )
            )
        return int(cast(CursorResult[Any], result).rowcount or 0)
