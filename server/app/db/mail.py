"""Domain ORM mappings: mail; registered once on shared Base.metadata."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class MailAttachmentRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    """MAIL-01 待发送附件：上传后短时有效，确认发送时消费，过期自动清理。

    个人单用户部署，内容直接落库（LargeBinary）；仅在预览与 SMTP 发送时读取。
    """

    __tablename__ = "mail_attachment"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','used','discarded')",
            name="ck_mail_attachment_status",
        ),
        Index("ix_mail_attachment_user_status", "user_id", "status"),
        Index("ix_mail_attachment_expires", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(
        String(127), nullable=False, default="application/octet-stream"
    )
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MailOutboxLogRecord(Base):
    # Preserve legacy mapper ordering and serialized class references.
    __module__ = "app.db.models"

    """MAIL-01 已发送邮件日志：确认发送成功后追加，永久保留。

    与 15 分钟 TTL 的 pending_mutation 不同，这是持久的本地发送凭据：
    收件人、主题、附件名与 SMTP 回执 message_id。正文不入库。
    """

    __tablename__ = "mail_outbox_log"
    __table_args__ = (Index("ix_mail_outbox_user_sent", "user_id", "sent_at"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    # 发起这封邮件的对话回合，用于回溯"是哪次对话让它发出的"
    turn_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    to_addresses: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    cc_addresses: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    attachments: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
