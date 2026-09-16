from .attachments import MailAttachmentStore
from .client import (
    MailAccount,
    MailAttachment,
    MailClient,
    MailError,
    MailSummary,
    resolve_mail_account,
)
from .tools import (
    MailAttachmentsTool,
    MailFoldersTool,
    MailMarkTool,
    MailMoveTool,
    MailReadTool,
    MailSendTool,
    create_mail_tools,
)

__all__ = [
    "MailAccount",
    "MailAttachment",
    "MailAttachmentStore",
    "MailAttachmentsTool",
    "MailClient",
    "MailError",
    "MailFoldersTool",
    "MailMarkTool",
    "MailMoveTool",
    "MailReadTool",
    "MailSendTool",
    "MailSummary",
    "create_mail_tools",
    "resolve_mail_account",
]
