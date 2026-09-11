"""SAFE-02 safety alert state machine.

Revision ID: 0038_safety_alerts
Revises: 0037_meetings
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0038_safety_alerts"
down_revision: str | None = "0037_meetings"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "safety_alert",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.String(length=160), nullable=False),
        sa.Column("entity_id", sa.String(length=255), nullable=False),
        sa.Column("severity", sa.String(length=12), nullable=False),
        sa.Column("message", sa.String(length=2_000), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("l1_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("l2_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ack_source", sa.String(length=32), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('escalating','acknowledged','expired')",
            name="ck_safety_alert_status",
        ),
        sa.CheckConstraint("severity IN ('critical')", name="ck_safety_alert_severity"),
        sa.CheckConstraint("level IN (1, 2)", name="ck_safety_alert_level"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_safety_alert_user_status_created",
        "safety_alert",
        ["user_id", "status", "created_at"],
    )
    op.create_index(
        "ix_safety_alert_active_dedupe",
        "safety_alert",
        ["user_id", "entity_id", "rule_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_safety_alert_active_dedupe", table_name="safety_alert")
    op.drop_index("ix_safety_alert_user_status_created", table_name="safety_alert")
    op.drop_table("safety_alert")
