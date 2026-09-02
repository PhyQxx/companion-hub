"""Add calendar_event table and task_item.source_ref for CAL-01.

Revision ID: 0030_calendar
Revises: 0029_daily_review
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030_calendar"
down_revision: str | None = "0029_daily_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "calendar_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("calendar_id", sa.String(length=64), nullable=False, server_default="primary"),
        sa.Column("title", sa.String(length=320), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("all_day", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("location", sa.String(length=240), nullable=True),
        sa.Column("participants", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="api"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("status IN ('active','cancelled')", name="ck_calendar_event_status"),
    )
    op.create_index(
        "ix_calendar_event_user_start",
        "calendar_event",
        ["user_id", "status", "starts_at"],
    )
    with op.batch_alter_table("task_item") as batch:
        batch.add_column(sa.Column("source_ref", sa.String(length=120), nullable=True))
    op.create_index("ix_task_item_source_ref", "task_item", ["source_ref"])


def downgrade() -> None:
    op.drop_index("ix_task_item_source_ref", table_name="task_item")
    with op.batch_alter_table("task_item") as batch:
        batch.drop_column("source_ref")
    op.drop_index("ix_calendar_event_user_start", table_name="calendar_event")
    op.drop_table("calendar_event")
