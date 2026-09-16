"""MAIL-01 attachments: pending mail attachments table.

Revision ID: 0042_mail_attachments
Revises: 0041_task_pending_priority
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0042_mail_attachments"
down_revision: str | None = "0041_task_pending_priority"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "mail_attachment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=127), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("payload", sa.LargeBinary(length=(2**32) - 1), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','used','discarded')",
            name="ck_mail_attachment_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mail_attachment_user_status",
        "mail_attachment",
        ["user_id", "status"],
    )
    op.create_index(
        "ix_mail_attachment_expires",
        "mail_attachment",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_mail_attachment_expires", table_name="mail_attachment")
    op.drop_index("ix_mail_attachment_user_status", table_name="mail_attachment")
    op.drop_table("mail_attachment")
