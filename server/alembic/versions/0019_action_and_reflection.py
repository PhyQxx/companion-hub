"""Add action results and reflection candidates for M3B v1 completion.

Revision ID: 0019_action_and_reflection
Revises: 0018_memory_job_fact_key
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019_action_and_reflection"
down_revision: str | None = "0018_memory_job_fact_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "action_result",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column("level", sa.String(length=4), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("reason_code", sa.String(length=160), nullable=True),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("observed_state", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('observed','prompted','blocked','executed','verified','unknown_outcome')",
            name="ck_action_result_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"], ["cognitive_decision.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_action_result_user_created", "action_result", ["user_id", "created_at"]
    )
    op.create_index(
        "ix_action_result_decision", "action_result", ["decision_id"]
    )

    op.create_table(
        "reflection_candidate",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "requires_confirmation", sa.Boolean(), nullable=False, server_default="true"
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','confirmed','rejected','superseded')",
            name="ck_reflection_candidate_status",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_reflection_candidate_user_created",
        "reflection_candidate",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_reflection_candidate_status",
        "reflection_candidate",
        ["user_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_reflection_candidate_status", table_name="reflection_candidate")
    op.drop_index("ix_reflection_candidate_user_created", table_name="reflection_candidate")
    op.drop_table("reflection_candidate")
    op.drop_index("ix_action_result_decision", table_name="action_result")
    op.drop_index("ix_action_result_user_created", table_name="action_result")
    op.drop_table("action_result")
