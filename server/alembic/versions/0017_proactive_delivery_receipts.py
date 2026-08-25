"""Add proactive multi-channel delivery receipts.

Revision ID: 0017_proactive_delivery_receipts
Revises: 0016_perception_pipeline
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_proactive_delivery_receipts"
down_revision: str | None = "0016_perception_pipeline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "proactive_delivery_receipt",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=True),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=160), nullable=True),
        sa.Column("external_operation_id", sa.String(length=160), nullable=True),
        sa.Column("privacy_level", sa.String(length=2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "channel IN ('web_chat','desktop_notification','voice')",
            name="ck_proactive_delivery_channel",
        ),
        sa.CheckConstraint(
            "status IN ('delivered','failed')",
            name="ck_proactive_delivery_status",
        ),
        sa.CheckConstraint(
            "privacy_level IN ('L0','L1','L2')",
            name="ck_proactive_delivery_privacy",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["decision_id"], ["cognitive_decision.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_proactive_delivery_user_created",
        "proactive_delivery_receipt",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_proactive_delivery_decision",
        "proactive_delivery_receipt",
        ["decision_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_proactive_delivery_decision", table_name="proactive_delivery_receipt"
    )
    op.drop_index(
        "ix_proactive_delivery_user_created", table_name="proactive_delivery_receipt"
    )
    op.drop_table("proactive_delivery_receipt")
