"""Add M3B cognitive decisions, goals, and feedback.

Revision ID: 0015_cognitive_cycle
Revises: 0014_home_assistant_proactive
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_cognitive_cycle"
down_revision: str | None = "0014_home_assistant_proactive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cognitive_decision",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("trigger_kind", sa.String(length=160), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("urgency", sa.String(length=16), nullable=False),
        sa.Column("attention_score", sa.Float(), nullable=False),
        sa.Column("policy_version", sa.String(length=80), nullable=False),
        sa.Column("model_provider", sa.String(length=80), nullable=True),
        sa.Column("model_name", sa.String(length=200), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('ignore','record','inform','ask','suggest','act','escalate')",
            name="ck_cognitive_decision_kind",
        ),
        sa.CheckConstraint(
            "urgency IN ('low','normal','high','critical')",
            name="ck_cognitive_decision_urgency",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_cognitive_decision_confidence",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversation.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cognitive_user_created", "cognitive_decision", ["user_id", "created_at"])
    op.create_index(
        "ix_cognitive_trigger_created", "cognitive_decision", ["trigger_kind", "created_at"]
    )
    op.create_table(
        "cognitive_goal",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=320), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source_kind", sa.String(length=40), nullable=False),
        sa.Column("source_id", sa.String(length=200), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('user','shared','system')", name="ck_cognitive_goal_kind"),
        sa.CheckConstraint(
            "status IN ('active','completed','cancelled','expired')",
            name="ck_cognitive_goal_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_cognitive_goal_user_status", "cognitive_goal", ["user_id", "status", "due_at"]
    )
    op.create_table(
        "cognitive_feedback",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('accepted','ignored','snoozed','forbidden')",
            name="ck_cognitive_feedback_kind",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["decision_id"], ["cognitive_decision.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_cognitive_feedback_user_created", "cognitive_feedback", ["user_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_cognitive_feedback_user_created", table_name="cognitive_feedback")
    op.drop_table("cognitive_feedback")
    op.drop_index("ix_cognitive_goal_user_status", table_name="cognitive_goal")
    op.drop_table("cognitive_goal")
    op.drop_index("ix_cognitive_trigger_created", table_name="cognitive_decision")
    op.drop_index("ix_cognitive_user_created", table_name="cognitive_decision")
    op.drop_table("cognitive_decision")
