"""Expand interaction_turn states and add runtime lease / user_mode tables.

Revision ID: 0020_turn_coordinator
Revises: 0019_action_and_reflection
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020_turn_coordinator"
down_revision: str | None = "0019_action_and_reflection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Expand interaction_turn state constraint
    with op.batch_alter_table("interaction_turn") as batch_op:
        batch_op.drop_constraint("ck_interaction_turn_state", type_="check")
        batch_op.create_check_constraint(
            "ck_interaction_turn_state",
            sa.text(
                "state IN ('accepted','listening','thinking','streaming','speaking',"
                "'interrupted','cancelled','failed','completed')"
            ),
        )
        batch_op.add_column(sa.Column("degradation", sa.JSON(), nullable=True))

    # Create runtime_lease table
    op.create_table(
        "runtime_lease",
        sa.Column("lease_type", sa.String(length=32), nullable=False),
        sa.Column("holder_device_id", sa.Uuid(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=True),
        sa.Column("epoch", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("lease_type"),
    )
    op.create_index("ix_runtime_lease_expires", "runtime_lease", ["expires_at"])

    # Create user_mode table
    op.create_table(
        "user_mode",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=160), nullable=False),
        sa.Column("reason", sa.String(length=400), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="50"),
        sa.Column(
            "starts_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_mode_user_active", "user_mode", ["user_id", "superseded_at", "priority"]
    )


def downgrade() -> None:
    op.drop_index("ix_user_mode_user_active", table_name="user_mode")
    op.drop_table("user_mode")
    op.drop_index("ix_runtime_lease_expires", table_name="runtime_lease")
    op.drop_table("runtime_lease")
    with op.batch_alter_table("interaction_turn") as batch_op:
        batch_op.drop_column("degradation")
        batch_op.drop_constraint("ck_interaction_turn_state", type_="check")
        batch_op.create_check_constraint(
            "ck_interaction_turn_state",
            sa.text(
                "state IN ('accepted','thinking','streaming','cancelled','failed','completed')"
            ),
        )
