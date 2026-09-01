"""Add action verification evidence and compensation plan links.

Revision ID: 0025_action_verification
Revises: 0024_action_plan
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_action_verification"
down_revision: str | None = "0024_action_plan"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("action_plan") as batch:
        batch.add_column(
            sa.Column(
                "plan_kind",
                sa.String(length=24),
                nullable=False,
                server_default="standard",
            )
        )
        batch.add_column(sa.Column("source_plan_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("undo_plan_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_action_plan_source_plan",
            "action_plan",
            ["source_plan_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_check_constraint(
            "ck_action_plan_kind",
            "plan_kind IN ('standard','compensation')",
        )
        batch.create_foreign_key(
            "fk_action_plan_undo_plan",
            "action_plan",
            ["undo_plan_id"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("action_step") as batch:
        batch.add_column(sa.Column("compensates_step_id", sa.Uuid(), nullable=True))
        batch.add_column(
            sa.Column(
                "verification_status",
                sa.String(length=24),
                nullable=False,
                server_default="pending",
            )
        )
        batch.add_column(sa.Column("verification_result", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key(
            "fk_action_step_compensates_step",
            "action_step",
            ["compensates_step_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_check_constraint(
            "ck_action_step_verification_status",
            "verification_status IN ('pending','not_required','verified','inconclusive')",
        )
    op.execute(
        "UPDATE action_step SET verification_status = CASE "
        "WHEN verification_policy = 'none' THEN 'not_required' ELSE 'inconclusive' END"
    )


def downgrade() -> None:
    with op.batch_alter_table("action_step") as batch:
        batch.drop_constraint("ck_action_step_verification_status", type_="check")
        batch.drop_constraint("fk_action_step_compensates_step", type_="foreignkey")
        batch.drop_column("verified_at")
        batch.drop_column("verification_result")
        batch.drop_column("verification_status")
        batch.drop_column("compensates_step_id")
    with op.batch_alter_table("action_plan") as batch:
        batch.drop_constraint("ck_action_plan_kind", type_="check")
        batch.drop_constraint("fk_action_plan_undo_plan", type_="foreignkey")
        batch.drop_constraint("fk_action_plan_source_plan", type_="foreignkey")
        batch.drop_column("undo_plan_id")
        batch.drop_column("source_plan_id")
        batch.drop_column("plan_kind")
