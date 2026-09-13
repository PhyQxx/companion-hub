"""SAFE-02 L3 escalation: pre-authorized contacts and escalation ledger.

Revision ID: 0039_safety_escalation
Revises: 0038_safety_alerts
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0039_safety_escalation"
down_revision: str | None = "0038_safety_alerts"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "safety_authorization",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("contact_name", sa.String(length=120), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("destination", sa.String(length=254), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active','revoked')", name="ck_safety_authorization_status"
        ),
        sa.CheckConstraint("channel IN ('email')", name="ck_safety_authorization_channel"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_safety_authorization_user_status",
        "safety_authorization",
        ["user_id", "status"],
    )
    op.create_table(
        "safety_alert_escalation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("alert_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("destination", sa.String(length=254), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('sent','failed','skipped')",
            name="ck_safety_alert_escalation_status",
        ),
        sa.CheckConstraint("level IN (3)", name="ck_safety_alert_escalation_level"),
        sa.ForeignKeyConstraint(["alert_id"], ["safety_alert.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_safety_alert_escalation_alert",
        "safety_alert_escalation",
        ["alert_id", "level"],
    )


def downgrade() -> None:
    op.drop_index("ix_safety_alert_escalation_alert", table_name="safety_alert_escalation")
    op.drop_table("safety_alert_escalation")
    op.drop_index("ix_safety_authorization_user_status", table_name="safety_authorization")
    op.drop_table("safety_authorization")
