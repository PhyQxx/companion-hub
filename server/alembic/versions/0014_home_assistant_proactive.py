"""add Home Assistant proactive notification ledger

Revision ID: 0014_home_assistant_proactive
Revises: 0013_device_command
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_home_assistant_proactive"
down_revision: str | None = "0013_device_command"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "home_assistant_proactive_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("entity_id", sa.String(length=255), nullable=False),
        sa.Column("rule_id", sa.String(length=80), nullable=False),
        sa.Column("trigger_kind", sa.String(length=40), nullable=False),
        sa.Column("passed_gate", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason_code", sa.String(length=160), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_ha_proactive_rule_created",
        "home_assistant_proactive_log",
        ["entity_id", "rule_id", "created_at"],
    )
    op.create_index(
        "ix_ha_proactive_user_created",
        "home_assistant_proactive_log",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ha_proactive_user_created", table_name="home_assistant_proactive_log")
    op.drop_index("ix_ha_proactive_rule_created", table_name="home_assistant_proactive_log")
    op.drop_table("home_assistant_proactive_log")
