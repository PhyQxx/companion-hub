"""create persisted interaction turn state

Revision ID: 0005_interaction_turn
Revises: 0004_chat_identity
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_interaction_turn"
down_revision: str | None = "0004_chat_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversation",
        sa.Column("last_turn_seq", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.create_table(
        "interaction_turn",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("turn_seq", sa.BigInteger(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("state_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("input_message_id", sa.Uuid(), nullable=False),
        sa.Column("cancel_reason", sa.String(length=160), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('accepted','thinking','streaming','cancelled','failed','completed')",
            name="ck_interaction_turn_state",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversation.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["input_message_id"], ["message.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "turn_seq", name="uq_turn_conversation_seq"),
        sa.UniqueConstraint("generation_id", name="uq_turn_generation"),
    )
    op.create_index(
        "ix_turn_conversation_state",
        "interaction_turn",
        ["conversation_id", "state", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_turn_conversation_state", table_name="interaction_turn")
    op.drop_table("interaction_turn")
    op.drop_column("conversation", "last_turn_seq")
