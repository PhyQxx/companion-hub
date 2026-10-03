"""Retain no-content proactive acceptance counts independently of decisions."""

import sqlalchemy as sa

from alembic import context, op

revision = "0065_proactive_quota"
down_revision = "0064_observation_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "proactive_quota_entry",
        sa.Column("decision_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_proactive_quota_user_accepted", "proactive_quota_entry", ["user_id", "accepted_at"]
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("proactive quota cannot be checked offline; downgrade refused")
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM proactive_quota_entry")):
        raise RuntimeError("proactive quota evidence must be retained; downgrade refused")
    op.drop_table("proactive_quota_entry")
