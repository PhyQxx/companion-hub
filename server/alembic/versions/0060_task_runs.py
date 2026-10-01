"""Track new task runs without rewriting historical turn/plan/job identifiers."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0060_task_runs"
down_revision = "0059_skill_verification_report"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_run",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "conversation_id", sa.Uuid(), sa.ForeignKey("conversation.id", ondelete="CASCADE")
        ),
        sa.Column("parent_run_id", sa.Uuid(), sa.ForeignKey("task_run.id", ondelete="SET NULL")),
        sa.Column("request_id", sa.String(160)),
        sa.Column("contract", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("cancel_epoch", sa.Integer(), nullable=False),
        sa.Column("privacy_level", sa.String(2), nullable=False),
        sa.Column("config_version", sa.Integer()),
        sa.Column("persona_version", sa.Integer()),
        sa.Column("deadline", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('accepted','running','succeeded','failed','cancelled')",
            name="ck_task_run_status",
        ),
        sa.CheckConstraint("privacy_level IN ('L0','L1','L2')", name="ck_task_run_privacy"),
        sa.UniqueConstraint("user_id", "request_id", name="uq_task_run_user_request"),
    )
    op.create_index("ix_task_run_user_created", "task_run", ["user_id", "created_at"])
    op.create_table(
        "task_run_event",
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("task_run.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(80), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("privacy_level", sa.String(2), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "seq", name="uq_task_run_event_seq"),
    )
    for table in ("interaction_turn", "action_plan", "job"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("task_run_id", sa.Uuid(), nullable=True))
            batch.create_foreign_key(
                f"fk_{table}_task_run", "task_run", ["task_run_id"], ["id"], ondelete="SET NULL"
            )
            batch.create_index(f"ix_{table}_task_run_id", ["task_run_id"])


def downgrade() -> None:
    for table in ("job", "action_plan", "interaction_turn"):
        with op.batch_alter_table(table) as batch:
            batch.drop_index(f"ix_{table}_task_run_id")
            batch.drop_constraint(f"fk_{table}_task_run", type_="foreignkey")
            batch.drop_column("task_run_id")
    op.drop_table("task_run_event")
    op.drop_index("ix_task_run_user_created", table_name="task_run")
    op.drop_table("task_run")
