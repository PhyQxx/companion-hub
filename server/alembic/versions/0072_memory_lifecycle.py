"""Add independently versioned memory strength and owned usage history."""

import sqlalchemy as sa

from alembic import op

revision = "0072_memory_lifecycle"
down_revision = "0071_activity_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in (
        sa.Column("tier", sa.String(16), nullable=False, server_default="medium"),
        sa.Column("tier_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("tier_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("next_decay_at", sa.DateTime(timezone=True)),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lifecycle_version", sa.Integer(), nullable=False, server_default="0"),
    ):
        op.add_column("memory", column)
    op.execute("UPDATE memory SET tier_changed_at=CURRENT_TIMESTAMP, tier_locked=pin")
    with op.batch_alter_table("memory") as batch:
        batch.alter_column(
            "tier_changed_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        )
        batch.create_index("ix_memory_next_decay_at", ["next_decay_at"])
    pk = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "memory_usage",
        sa.Column("id", pk, primary_key=True, autoincrement=True),
        sa.Column(
            "memory_id",
            sa.BigInteger(),
            sa.ForeignKey("memory.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("turn_key", sa.String(160), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("uq_memory_usage_turn", "memory_usage", ["memory_id", "turn_key"], unique=True)
    op.create_table(
        "memory_lifecycle_event",
        sa.Column("id", pk, primary_key=True, autoincrement=True),
        sa.Column(
            "memory_id",
            sa.BigInteger(),
            sa.ForeignKey("memory.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("old_tier", sa.String(16), nullable=False),
        sa.Column("new_tier", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("policy_version", sa.String(40), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_memory_lifecycle_event_memory_id", "memory_lifecycle_event", ["memory_id"])


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT COUNT(*) FROM memory")):
        raise RuntimeError(
            "Memory history must be retained; disable lifecycle flags instead of dropping data"
        )
    op.drop_table("memory_lifecycle_event")
    op.drop_table("memory_usage")
    op.drop_index("ix_memory_next_decay_at", table_name="memory")
    for column in (
        "tier",
        "tier_locked",
        "tier_changed_at",
        "last_used_at",
        "next_decay_at",
        "use_count",
        "lifecycle_version",
    ):
        op.drop_column("memory", column)
