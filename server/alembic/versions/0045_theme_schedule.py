"""Theme scheduling: appearance_mode gains 'scheduled' with light/dark boundaries.

Revision ID: 0045_theme_schedule
Revises: 0044_google_oauth_token
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0045_theme_schedule"
down_revision: str | None = "0044_google_oauth_token"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("ui_preference") as batch:
        batch.add_column(sa.Column("schedule_light_time", sa.String(length=5), nullable=True))
        batch.add_column(sa.Column("schedule_dark_time", sa.String(length=5), nullable=True))
        batch.drop_constraint("ck_ui_preference_mode", type_="check")
        batch.create_check_constraint(
            "ck_ui_preference_mode",
            "appearance_mode IN ('light','dark','system','scheduled')",
        )


def downgrade() -> None:
    with op.batch_alter_table("ui_preference") as batch:
        batch.drop_constraint("ck_ui_preference_mode", type_="check")
        batch.create_check_constraint(
            "ck_ui_preference_mode",
            "appearance_mode IN ('light','dark','system')",
        )
        batch.drop_column("schedule_dark_time")
        batch.drop_column("schedule_light_time")
