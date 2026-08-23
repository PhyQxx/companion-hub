"""create device registry and one-time pairing codes

Revision ID: 0012_device_registry
Revises: 0011_timeline_event
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_device_registry"
down_revision: str | None = "0011_timeline_event"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "device_client",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "owner_user_id",
            sa.Uuid(),
            sa.ForeignKey("app_user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("alias", sa.String(length=80), nullable=True),
        sa.Column("client_type", sa.String(length=32), nullable=False),
        sa.Column("credential_hash", sa.String(length=64), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("granted_capabilities", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("paired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("credential_hash", name="uq_device_client_credential_hash"),
        sa.UniqueConstraint("owner_user_id", "alias", name="uq_device_client_owner_alias"),
    )
    op.create_index(
        "ix_device_client_owner_seen",
        "device_client",
        ["owner_user_id", "revoked_at", "last_seen_at"],
    )
    op.create_table(
        "device_pairing_code",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "owner_user_id",
            sa.Uuid(),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("granted_capabilities", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "claimed_device_id",
            sa.Uuid(),
            sa.ForeignKey("device_client.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint("code_hash", name="uq_device_pairing_code_hash"),
    )
    op.create_index(
        "ix_device_pairing_code_expiry",
        "device_pairing_code",
        ["expires_at", "claimed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_device_pairing_code_expiry", table_name="device_pairing_code")
    op.drop_table("device_pairing_code")
    op.drop_index("ix_device_client_owner_seen", table_name="device_client")
    op.drop_table("device_client")
