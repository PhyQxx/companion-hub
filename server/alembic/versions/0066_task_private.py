"""Preserve private meeting action tasks without allowing ephemeral persistence."""

import sqlalchemy as sa

from alembic import context, op

revision = "0066_task_private"
down_revision = "0065_proactive_quota"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("task_item") as batch:
        batch.drop_constraint("ck_task_item_privacy_level", type_="check")
        batch.create_check_constraint(
            "ck_task_item_privacy_level", "privacy_level IN ('L0','L1','L2')"
        )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("private task privacy cannot be checked offline; downgrade refused")
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM task_item WHERE privacy_level = 'L2'")):
        raise RuntimeError("private task privacy evidence must be retained; downgrade refused")
    with op.batch_alter_table("task_item") as batch:
        batch.drop_constraint("ck_task_item_privacy_level", type_="check")
        batch.create_check_constraint("ck_task_item_privacy_level", "privacy_level IN ('L0','L1')")
