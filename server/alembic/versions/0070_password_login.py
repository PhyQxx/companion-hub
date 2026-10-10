"""Username login: unique local account name on app_user."""

import sqlalchemy as sa

from alembic import context, op

revision = "0070_password_login"
down_revision = "0069_multiuser_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("app_user") as batch:
        batch.add_column(sa.Column("username", sa.String(64), nullable=True))
        batch.create_unique_constraint("uq_app_user_username", ["username"])


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("username login cannot be checked offline; downgrade refused")
    query = sa.text("SELECT count(*) FROM app_user WHERE username IS NOT NULL")
    named = op.get_bind().scalar(query)
    if named:
        raise RuntimeError("username bindings must be retained; downgrade refused")
    with op.batch_alter_table("app_user") as batch:
        batch.drop_constraint("uq_app_user_username", type_="unique")
        batch.drop_column("username")
