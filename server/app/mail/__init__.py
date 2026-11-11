"""Public mail exports; implementations load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .attachments import MailAttachmentStore
    from .client import (
        MailAccount,
        MailAttachment,
        MailClient,
        MailError,
        MailSummary,
        resolve_mail_account,
    )
    from .outbox import MailOutboxStore
    from .tools import (
        MailAttachmentsTool,
        MailFoldersTool,
        MailMarkTool,
        MailMoveTool,
        MailReadTool,
        MailSendTool,
        MailSentTool,
        create_mail_tools,
    )

_EXPORTS = {
    "MailAccount": ("app.mail.client", "MailAccount"),
    "MailAttachment": ("app.mail.client", "MailAttachment"),
    "MailAttachmentStore": ("app.mail.attachments", "MailAttachmentStore"),
    "MailAttachmentsTool": ("app.mail.tools", "MailAttachmentsTool"),
    "MailClient": ("app.mail.client", "MailClient"),
    "MailError": ("app.mail.client", "MailError"),
    "MailFoldersTool": ("app.mail.tools", "MailFoldersTool"),
    "MailMarkTool": ("app.mail.tools", "MailMarkTool"),
    "MailMoveTool": ("app.mail.tools", "MailMoveTool"),
    "MailOutboxStore": ("app.mail.outbox", "MailOutboxStore"),
    "MailReadTool": ("app.mail.tools", "MailReadTool"),
    "MailSendTool": ("app.mail.tools", "MailSendTool"),
    "MailSentTool": ("app.mail.tools", "MailSentTool"),
    "MailSummary": ("app.mail.client", "MailSummary"),
    "create_mail_tools": ("app.mail.tools", "create_mail_tools"),
    "resolve_mail_account": ("app.mail.client", "resolve_mail_account"),
}

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
    "MailOutboxStore",
    "MailReadTool",
    "MailSendTool",
    "MailSentTool",
    "MailSummary",
    "create_mail_tools",
    "resolve_mail_account",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
