"""create reliable event bus tables

Revision ID: 0001_event_outbox
Revises:
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_event_outbox"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("proto_version", sa.Integer(), nullable=False),
        sa.Column("schema_ref", sa.String(length=200), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("causation_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("turn_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.JSON(), nullable=False),
        sa.Column("type", sa.String(length=160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("privacy_level", sa.String(length=2), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("privacy_level IN ('L0','L1','L2')", name="ck_event_privacy_level"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_event_conversation_created", "event", ["conversation_id", "created_at"])
    op.create_index("ix_event_correlation", "event", ["correlation_id"])

    op.create_table(
        "outbox",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("topic", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=160), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','sending','dispatched','dead')", name="ck_outbox_status"
        ),
        sa.ForeignKeyConstraint(["event_id"], ["event.event_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "topic", name="uq_outbox_event_topic"),
    )
    op.create_index(
        "ix_outbox_dispatch",
        "outbox",
        ["status", "available_at", "lease_expires_at", "id"],
    )

    op.create_table(
        "consumer_inbox",
        sa.Column("consumer_name", sa.String(length=160), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column(
            "processed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["event_id"], ["event.event_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("consumer_name", "event_id"),
    )

    op.create_table(
        "dead_letter",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("outbox_id", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("topic", sa.String(length=200), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=160), nullable=False),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["outbox_id"], ["outbox.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("outbox_id", name="uq_dead_letter_outbox"),
    )
    op.create_index("ix_dead_letter_created", "dead_letter", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_dead_letter_created", table_name="dead_letter")
    op.drop_table("dead_letter")
    op.drop_table("consumer_inbox")
    op.drop_index("ix_outbox_dispatch", table_name="outbox")
    op.drop_table("outbox")
    op.drop_index("ix_event_correlation", table_name="event")
    op.drop_index("ix_event_conversation_created", table_name="event")
    op.drop_table("event")
