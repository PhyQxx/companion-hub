"""Retain the first owner of global private observation sources."""

import sqlalchemy as sa

from alembic import context, op

revision = "0064_observation_owner"
down_revision = "0063_goal_privacy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "observation_owner_binding",
        sa.Column("slot", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("slot = 1", name="ck_observation_owner_binding_slot"),
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("observation ownership cannot be checked offline; downgrade refused")
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM observation_owner_binding")):
        raise RuntimeError("observation ownership evidence must be retained; downgrade refused")
    op.drop_table("observation_owner_binding")
