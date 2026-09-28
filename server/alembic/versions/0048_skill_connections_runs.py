"""Admin-owned Skill connections and metadata-only execution runs.

Revision ID: 0048_skill_connections_runs
Revises: 0047_skill_versions
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0048_skill_connections_runs"
down_revision: str | None = "0047_skill_versions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "skill_connection",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("base_url", sa.String(500), nullable=False),
        sa.Column("auth_type", sa.String(16), nullable=False),
        sa.Column("secret_ref", sa.String(132), nullable=True),
        sa.Column("header_name", sa.String(80), nullable=True),
        sa.Column("allowed_paths", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "skill_run",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "skill_id", sa.Uuid(), sa.ForeignKey("skill.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("skill_version", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.String(64), nullable=False),
        sa.Column("operation", sa.String(50), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_skill_run_skill_created", "skill_run", ["skill_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_skill_run_skill_created", table_name="skill_run")
    op.drop_table("skill_run")
    op.drop_table("skill_connection")
