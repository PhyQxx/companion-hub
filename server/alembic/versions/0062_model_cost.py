"""Content-free estimated cost ledger independent of source retention."""

import sqlalchemy as sa

from alembic import context, op

revision = "0062_model_cost"
down_revision = "0061_model_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_cost",
        sa.Column("call_id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.String(160), nullable=False),
        sa.Column("currency", sa.String(3)),
        sa.Column("input_rate", sa.Numeric(24, 12)),
        sa.Column("output_rate", sa.Numeric(24, 12)),
        sa.Column("reserved_micros", sa.BigInteger()),
        sa.Column("charged_micros", sa.BigInteger()),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("provider_request_id", sa.String(200)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "state IN ('reserved','estimated','unknown')", name="ck_model_cost_state"
        ),
        sa.CheckConstraint(
            "charged_micros IS NULL OR charged_micros >= 0", name="ck_model_cost_charge"
        ),
    )
    op.create_index("ix_model_cost_user_created", "model_cost", ["user_id", "created_at"])


def downgrade() -> None:
    # Keep accounting across application rollback; an empty test ledger can downgrade.
    if not context.is_offline_mode() and op.get_bind().scalar(
        sa.text("SELECT count(*) FROM model_cost")
    ):
        raise RuntimeError("model_cost ledger must be retained; downgrade refused")
    op.drop_index("ix_model_cost_user_created", table_name="model_cost")
    op.drop_table("model_cost")
