"""Free device aliases on revoked devices via partial unique index.

Revision ID: 0023_device_alias_reuse
Revises: 0022_ui_theme
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023_device_alias_reuse"
down_revision: str | None = "0022_ui_theme"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("device_client") as batch:
        batch.drop_constraint("uq_device_client_owner_alias", type_="unique")
    op.create_index(
        "uq_device_client_owner_alias",
        "device_client",
        ["owner_user_id", "alias"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
        sqlite_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_device_client_owner_alias", table_name="device_client")
    with op.batch_alter_table("device_client") as batch:
        batch.create_unique_constraint("uq_device_client_owner_alias", ["owner_user_id", "alias"])
