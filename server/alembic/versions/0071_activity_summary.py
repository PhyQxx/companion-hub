"""Daily and weekly activity summary snapshots."""

import sqlalchemy as sa

from alembic import context, op

revision = "0071_activity_summary"
down_revision = "0070_password_login"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "activity_summary",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "kind", "period_start", name="uq_activity_summary_period"),
    )


def downgrade() -> None:
    if context.is_offline_mode() or op.get_bind().scalar(
        sa.text("SELECT count(*) FROM activity_summary")
    ):
        raise RuntimeError("saved activity summaries must be retained; downgrade refused")
    op.drop_table("activity_summary")
