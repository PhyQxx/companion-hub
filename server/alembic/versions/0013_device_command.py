"""create durable device command ledger

Revision ID: 0013_device_command
Revises: 0012_device_registry
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_device_command"
down_revision: str | None = "0012_device_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "device_command",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "device_id",
            sa.Uuid(),
            sa.ForeignKey("device_client.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("command_name", sa.String(length=160), nullable=False),
        sa.Column("args_redacted", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason_code", sa.String(length=160), nullable=True),
        sa.Column("result_meta", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','sent','acknowledged','succeeded','failed','cancelled',"
            "'expired','timed_out')",
            name="ck_device_command_status",
        ),
        sa.UniqueConstraint(
            "device_id", "idempotency_key", name="uq_device_command_idempotency"
        ),
    )
    op.create_index(
        "ix_device_command_status_expiry",
        "device_command",
        ["device_id", "status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_device_command_status_expiry", table_name="device_command")
    op.drop_table("device_command")
