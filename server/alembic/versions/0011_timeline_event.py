"""create timeline event index

Revision ID: 0011_timeline_event
Revises: 0010_memory_subject_scope
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_timeline_event"
down_revision: str | None = "0010_memory_subject_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "timeline_event",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("source_id", sa.String(length=200), nullable=False),
        sa.Column("actor", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=160), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=240), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("privacy_level", sa.String(length=2), nullable=False),
        sa.Column("importance", sa.Float(), nullable=False, server_default="0.3"),
        sa.Column("entities", sa.JSON(), nullable=False),
        sa.Column("keywords", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("embedding", sa.JSON(), nullable=True),
        sa.Column("embedding_model", sa.String(length=160), nullable=True),
        sa.Column("embedding_version", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "source_type IN ('message','event','device','tool','calendar','system')",
            name="ck_timeline_source_type",
        ),
        sa.CheckConstraint(
            "actor IN ('user','assistant','device','system','external')",
            name="ck_timeline_actor",
        ),
        sa.CheckConstraint(
            "privacy_level IN ('L0','L1','L2')",
            name="ck_timeline_privacy",
        ),
        sa.CheckConstraint(
            "importance >= 0 AND importance <= 1",
            name="ck_timeline_importance",
        ),
        sa.UniqueConstraint(
            "source_type",
            "source_id",
            "event_type",
            name="uq_timeline_source_event",
        ),
    )
    op.create_index(
        "ix_timeline_user_occurred", "timeline_event", ["user_id", "occurred_at"]
    )
    op.create_index(
        "ix_timeline_user_event_occurred",
        "timeline_event",
        ["user_id", "event_type", "occurred_at"],
    )
    op.create_index(
        "ix_timeline_user_actor_occurred",
        "timeline_event",
        ["user_id", "actor", "occurred_at"],
    )
    op.create_index(
        "ix_timeline_source", "timeline_event", ["source_type", "source_id"]
    )
    op.create_index(
        "ix_timeline_conversation",
        "timeline_event",
        ["conversation_id", "occurred_at"],
    )

    # Timeline is a derived index. Backfill existing durable sources so an
    # upgrade does not make pre-0011 history invisible. L2 rows deliberately
    # keep only an opaque index shell; raw text remains in the source table.
    op.execute(
        """
        INSERT INTO timeline_event (
            user_id, occurred_at, ended_at, source_type, source_id, actor,
            event_type, conversation_id, title, summary, privacy_level,
            importance, entities, keywords, metadata, embedding,
            embedding_model, embedding_version, created_at
        )
        SELECT
            conversation.user_id,
            message.created_at,
            NULL,
            'message',
            CAST(message.id AS VARCHAR),
            CASE
                WHEN message.role = 'user' THEN 'user'
                WHEN message.role = 'assistant' THEN 'assistant'
                ELSE 'system'
            END,
            'conversation.message',
            message.conversation_id,
            NULL,
            CASE
                WHEN message.privacy_level = 'L2' THEN 'L2 对话消息'
                ELSE substr(message.content, 1, 320)
            END,
            message.privacy_level,
            0.25,
            '[]',
            '[]',
            '{}',
            NULL,
            NULL,
            NULL,
            message.created_at
        FROM message
        JOIN conversation ON conversation.id = message.conversation_id
        """
    )
    op.execute(
        """
        INSERT INTO timeline_event (
            user_id, occurred_at, ended_at, source_type, source_id, actor,
            event_type, conversation_id, title, summary, privacy_level,
            importance, entities, keywords, metadata, embedding,
            embedding_model, embedding_version, created_at
        )
        SELECT
            event.user_id,
            event.occurred_at,
            NULL,
            'event',
            CAST(event.event_id AS VARCHAR),
            CASE
                WHEN event.type LIKE 'device.%' THEN 'device'
                WHEN event.type LIKE 'system.%' THEN 'system'
                ELSE 'external'
            END,
            event.type,
            event.conversation_id,
            NULL,
            CASE
                WHEN event.privacy_level = 'L2' THEN 'L2 事件 ' || event.type
                ELSE event.type
            END,
            event.privacy_level,
            0.35,
            '[]',
            '[]',
            '{}',
            NULL,
            NULL,
            NULL,
            event.created_at
        FROM event
        """
    )


def downgrade() -> None:
    op.drop_index("ix_timeline_conversation", table_name="timeline_event")
    op.drop_index("ix_timeline_source", table_name="timeline_event")
    op.drop_index("ix_timeline_user_actor_occurred", table_name="timeline_event")
    op.drop_index("ix_timeline_user_event_occurred", table_name="timeline_event")
    op.drop_index("ix_timeline_user_occurred", table_name="timeline_event")
    op.drop_table("timeline_event")
