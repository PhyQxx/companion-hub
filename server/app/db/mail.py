"""迁移期兼容重导出：mail 映射已迁至 app.mail.persistence。"""

from app.mail.persistence import MailAttachmentRecord, MailOutboxLogRecord

__all__ = ["MailAttachmentRecord", "MailOutboxLogRecord"]
