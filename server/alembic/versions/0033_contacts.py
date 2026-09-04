"""Add contact table for CONTACT-01.

Revision ID: 0033_contacts
Revises: 0032_web_push
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0033_contacts"
down_revision: str | None = "0032_web_push"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contact",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("relationship", sa.String(length=120), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("important_dates", sa.JSON(), nullable=False),
        sa.Column("preferences", sa.JSON(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_contact_user", "contact", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_contact_user", table_name="contact")
    op.drop_table("contact")
