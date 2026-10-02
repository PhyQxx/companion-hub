"""Preserve declared goal privacy alongside inherited source evidence."""

import sqlalchemy as sa

from alembic import context, op

revision = "0063_goal_privacy"
down_revision = "0062_model_cost"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("cognitive_goal") as batch:
        batch.add_column(sa.Column("privacy_level", sa.String(2), nullable=True))
        batch.create_check_constraint(
            "ck_cognitive_goal_privacy",
            "privacy_level IS NULL OR privacy_level IN ('L0','L1','L2')",
        )


def downgrade() -> None:
    if not context.is_offline_mode() and op.get_bind().scalar(
        sa.text("SELECT count(*) FROM cognitive_goal WHERE privacy_level IS NOT NULL")
    ):
        raise RuntimeError("goal privacy evidence must be retained; downgrade refused")
    with op.batch_alter_table("cognitive_goal") as batch:
        batch.drop_constraint("ck_cognitive_goal_privacy", type_="check")
        batch.drop_column("privacy_level")
