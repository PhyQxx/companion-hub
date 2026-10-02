"""Durable per-run model admission and content-free usage reservations."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0061_model_budget"
down_revision = "0060_task_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("task_run") as batch:
        batch.add_column(sa.Column("budget", sa.JSON()))
        batch.add_column(
            sa.Column("llm_attempts", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("budget_tokens", sa.BigInteger(), nullable=False, server_default="0")
        )
    op.create_table(
        "model_reservation",
        sa.Column("call_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("task_run.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("endpoint", sa.String(160), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("reserved_tokens", sa.BigInteger(), nullable=False),
        sa.Column("charged_tokens", sa.BigInteger(), nullable=False),
        sa.Column("actual_tokens", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "state IN ('reserved','settled','unknown')", name="ck_model_reservation_state"
        ),
        sa.CheckConstraint(
            "reserved_tokens > 0 AND charged_tokens >= 0", name="ck_model_reservation_tokens"
        ),
        sa.CheckConstraint(
            "phase IN ('interactive','maintenance')", name="ck_model_reservation_phase"
        ),
    )
    op.create_index(
        "ix_model_reservation_run_created", "model_reservation", ["run_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_model_reservation_run_created", table_name="model_reservation")
    op.drop_table("model_reservation")
    with op.batch_alter_table("task_run") as batch:
        batch.drop_column("budget_tokens")
        batch.drop_column("llm_attempts")
        batch.drop_column("budget")
