"""CAL-01 external calendar mirror columns.

Revision ID: 0043_calendar_mirror
Revises: 0042_mail_attachments
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0043_calendar_mirror"
down_revision: str | None = "0042_mail_attachments"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "calendar_event",
        sa.Column("source_ref", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "calendar_event",
        sa.Column("external_etag", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_calendar_event_source_ref",
        "calendar_event",
        ["user_id", "source", "source_ref"],
    )


def downgrade() -> None:
    op.drop_index("ix_calendar_event_source_ref", table_name="calendar_event")
    op.drop_column("calendar_event", "external_etag")
    op.drop_column("calendar_event", "source_ref")
