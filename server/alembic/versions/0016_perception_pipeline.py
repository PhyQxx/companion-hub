"""Add privacy-safe semantic event audit records.

Revision ID: 0016_perception_pipeline
Revises: 0015_cognitive_cycle
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_perception_pipeline"
down_revision: str | None = "0015_cognitive_cycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "semantic_event_audit",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=160), nullable=False),
        sa.Column("source_kind", sa.String(length=80), nullable=False),
        sa.Column("dedupe_key", sa.String(length=240), nullable=False),
        sa.Column("privacy_level", sa.String(length=2), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("disposition", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=160), nullable=True),
        sa.Column("decision_id", sa.Uuid(), nullable=True),
        sa.Column("merged_into_event_id", sa.Uuid(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "disposition IN ('processed','suppressed','merged','expired','unstable')",
            name="ck_semantic_event_disposition",
        ),
        sa.CheckConstraint(
            "privacy_level IN ('L0','L1','L2')",
            name="ck_semantic_event_privacy",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["decision_id"], ["cognitive_decision.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_semantic_event_user_created",
        "semantic_event_audit",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_semantic_event_user_dedupe_created",
        "semantic_event_audit",
        ["user_id", "dedupe_key", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_semantic_event_user_dedupe_created", table_name="semantic_event_audit")
    op.drop_index("ix_semantic_event_user_created", table_name="semantic_event_audit")
    op.drop_table("semantic_event_audit")
