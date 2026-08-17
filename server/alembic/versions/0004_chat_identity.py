"""create local chat credentials and short-lived sessions

Revision ID: 0004_chat_identity
Revises: 0003_text_chat
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_chat_identity"
down_revision: str | None = "0003_text_chat"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_credential",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("credential_id", sa.LargeBinary(), nullable=True),
        sa.Column("public_data", sa.JSON(), nullable=True),
        sa.Column("secret_hash", sa.Text(), nullable=True),
        sa.Column("setup_slot", sa.Integer(), nullable=True),
        sa.Column("params_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('password','passkey')", name="ck_auth_credential_kind"
        ),
        sa.CheckConstraint(
            "setup_slot IS NULL OR setup_slot = 1", name="ck_auth_credential_setup_slot"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("setup_slot", name="uq_auth_credential_setup_slot"),
    )
    op.create_index(
        "ix_auth_credential_user", "auth_credential", ["user_id", "revoked_at"]
    )
    op.create_table(
        "auth_session",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=True),
        sa.Column("access_hash", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("access_hash", name="uq_auth_session_access_hash"),
    )
    op.create_index(
        "ix_auth_session_user_active",
        "auth_session",
        ["user_id", "expires_at", "revoked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_auth_session_user_active", table_name="auth_session")
    op.drop_table("auth_session")
    op.drop_index("ix_auth_credential_user", table_name="auth_credential")
    op.drop_table("auth_credential")
