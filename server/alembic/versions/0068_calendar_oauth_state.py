"""Durable, session-bound single-use Google authorization requests."""

import sqlalchemy as sa

from alembic import context, op

revision = "0068_calendar_oauth_state"
down_revision = "0067_unit_costs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calendar_oauth_state",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("config_version", sa.BigInteger(), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["auth_session.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_calendar_oauth_state_user"),
        sa.UniqueConstraint("state_hash", name="uq_calendar_oauth_state_hash"),
        sa.CheckConstraint("expires_at > created_at", name="ck_calendar_oauth_state_expiry"),
        sa.CheckConstraint(
            "completed_at IS NULL OR consumed_at IS NOT NULL",
            name="ck_calendar_oauth_state_completed",
        ),
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("authorization state cannot be checked offline; downgrade refused")
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM calendar_oauth_state")):
        raise RuntimeError("authorization state must be retained; downgrade refused")
    op.drop_table("calendar_oauth_state")
