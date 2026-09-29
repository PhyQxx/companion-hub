"""CTX rolling conversation summary columns.

Revision ID: 0057_conversation_summary
Revises: 0056_mail_outbox_log
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0057_conversation_summary"
down_revision: str | None = "0056_mail_outbox_log"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("conversation", sa.Column("summary_text", sa.Text(), nullable=True))
    op.add_column(
        "conversation", sa.Column("summary_until_seq", sa.BigInteger(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("conversation", "summary_until_seq")
    op.drop_column("conversation", "summary_text")
