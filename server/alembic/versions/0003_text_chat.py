"""create trusted text chat tables

Revision ID: 0003_text_chat
Revises: 0002_config_versions
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_text_chat"
down_revision: str | None = "0002_config_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("locale", sa.String(length=32), server_default="zh-CN", nullable=False),
        sa.Column(
            "timezone", sa.String(length=64), server_default="Asia/Shanghai", nullable=False
        ),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "conversation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("last_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_active_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('active','archived')", name="ck_conversation_status"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conversation_user_active", "conversation", ["user_id", "last_active_at"]
    )
    op.create_table(
        "message",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("privacy_level", sa.String(length=2), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=True),
        sa.Column("decision_meta", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "role IN ('user','assistant','system','tool')", name="ck_message_role"
        ),
        sa.CheckConstraint(
            "privacy_level IN ('L0','L1','L2')", name="ck_message_privacy"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversation.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "seq", name="uq_message_conversation_seq"),
    )
    op.create_index("ix_message_conversation_seq", "message", ["conversation_id", "seq"])


def downgrade() -> None:
    op.drop_index("ix_message_conversation_seq", table_name="message")
    op.drop_table("message")
    op.drop_index("ix_conversation_user_active", table_name="conversation")
    op.drop_table("conversation")
    op.drop_table("app_user")
