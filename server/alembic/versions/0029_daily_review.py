"""Add daily_review table for REVIEW-01 evening review.

Revision ID: 0029_daily_review
Revises: 0028_daily_brief
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0029_daily_review"
down_revision: str | None = "0028_daily_brief"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "daily_review",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("review_date", sa.Date(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("channels", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("status IN ('pending','delivered')", name="ck_daily_review_status"),
        sa.UniqueConstraint("user_id", "review_date", name="uq_daily_review_user_date"),
    )
    op.create_index("ix_daily_review_user_created", "daily_review", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_daily_review_user_created", table_name="daily_review")
    op.drop_table("daily_review")
