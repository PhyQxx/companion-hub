"""MAIL-01 persistent local log for confirmed SMTP sends.

Revision ID: 0056_mail_outbox_log
Revises: 0055_skill_source_markdown
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0056_mail_outbox_log"
down_revision: str | None = "0055_skill_source_markdown"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "mail_outbox_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("to_addresses", sa.JSON(), nullable=False),
        sa.Column("cc_addresses", sa.JSON(), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("message_id", sa.String(length=255), nullable=False),
        sa.Column("attachments", sa.JSON(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mail_outbox_user_sent",
        "mail_outbox_log",
        ["user_id", "sent_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_mail_outbox_user_sent", table_name="mail_outbox_log")
    op.drop_table("mail_outbox_log")
