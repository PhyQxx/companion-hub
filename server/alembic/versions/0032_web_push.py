"""Add web push subscriptions and web_push delivery channel.

Revision ID: 0032_web_push
Revises: 0031_todo_sync
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0032_web_push"
down_revision: str | None = "0031_todo_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "push_subscription",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.String(length=768), nullable=False),
        sa.Column("p256dh", sa.String(length=255), nullable=False),
        sa.Column("auth", sa.String(length=255), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_push_subscription_failures",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("endpoint", name="uq_push_subscription_endpoint"),
    )
    op.create_index(
        "ix_push_subscription_user",
        "push_subscription",
        ["user_id"],
    )
    with op.batch_alter_table("proactive_delivery_receipt") as batch:
        batch.drop_constraint("ck_proactive_delivery_channel", type_="check")
        batch.create_check_constraint(
            "ck_proactive_delivery_channel",
            "channel IN ('web_chat','desktop_notification','web_push','voice')",
        )


def downgrade() -> None:
    with op.batch_alter_table("proactive_delivery_receipt") as batch:
        batch.drop_constraint("ck_proactive_delivery_channel", type_="check")
        batch.create_check_constraint(
            "ck_proactive_delivery_channel",
            "channel IN ('web_chat','desktop_notification','voice')",
        )
    op.drop_index("ix_push_subscription_user", table_name="push_subscription")
    op.drop_table("push_subscription")
