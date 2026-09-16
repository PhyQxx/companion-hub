"""CAL-01 Google calendar OAuth refresh token storage.

Revision ID: 0044_google_oauth_token
Revises: 0043_calendar_mirror
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0044_google_oauth_token"
down_revision: str | None = "0043_calendar_mirror"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "calendar_oauth_token",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=False),
        sa.Column("account_email", sa.String(length=254), nullable=True),
        sa.Column("obtained_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "provider", name="uq_calendar_oauth_user_provider"),
    )


def downgrade() -> None:
    op.drop_table("calendar_oauth_token")
