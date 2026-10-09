"""Multi-user identity: pnkx SSO binding and owner/member role."""

import sqlalchemy as sa

from alembic import context, op

revision = "0069_multiuser_identity"
down_revision = "0068_calendar_oauth_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("app_user") as batch:
        batch.add_column(sa.Column("sso_sub", sa.String(128), nullable=True))
        batch.add_column(
            sa.Column("role", sa.String(16), nullable=False, server_default="member")
        )
        batch.create_check_constraint("ck_app_user_role", "role IN ('owner','member')")
        batch.create_unique_constraint("uq_app_user_sso_sub", ["sso_sub"])
    # 既有部署为单用户中枢：升级前的活跃用户就是业主，其全部数据与本地
    # 密码凭据随迁；此后新用户一律由 SSO 白名单以 member 角色开户。
    op.execute("UPDATE app_user SET role = 'owner' WHERE status = 'active'")


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("user identity cannot be checked offline; downgrade refused")
    bound = op.get_bind().scalar(sa.text("SELECT count(*) FROM app_user WHERE sso_sub IS NOT NULL"))
    if bound:
        raise RuntimeError("sso identity bindings must be retained; downgrade refused")
    with op.batch_alter_table("app_user") as batch:
        batch.drop_constraint("uq_app_user_sso_sub", type_="unique")
        batch.drop_constraint("ck_app_user_role", type_="check")
        batch.drop_column("role")
        batch.drop_column("sso_sub")
