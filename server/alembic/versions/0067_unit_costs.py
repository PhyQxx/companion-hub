"""Retain unit pricing independently of content, alongside token accounting."""

import sqlalchemy as sa

from alembic import context, op

revision = "0067_unit_costs"
down_revision = "0066_task_private"
branch_labels = None
depends_on = None

_CHECKS = {
    "ck_model_cost_unit": "unit IS NULL OR unit IN ('request','image','second','character','byte')",
    "ck_model_cost_unit_quote": (
        "(unit IS NULL AND unit_rate IS NULL AND unit_maximum_quantity IS NULL "
        "AND unit_quantity IS NULL) OR (unit IS NOT NULL AND currency IS NOT NULL "
        "AND unit_rate IS NOT NULL AND unit_maximum_quantity IS NOT NULL "
        "AND input_rate IS NULL AND output_rate IS NULL)"
    ),
    "ck_model_cost_unit_rate": (
        "unit_rate IS NULL OR "
        "(CAST(unit_rate AS NUMERIC) >= 0 AND CAST(unit_rate AS NUMERIC) <= 1000000000)"
    ),
    "ck_model_cost_unit_maximum": (
        "unit_maximum_quantity IS NULL OR "
        "(CAST(unit_maximum_quantity AS NUMERIC) > 0 "
        "AND CAST(unit_maximum_quantity AS NUMERIC) <= 1000000000)"
    ),
    "ck_model_cost_unit_quantity": (
        "unit_quantity IS NULL OR "
        "(CAST(unit_quantity AS NUMERIC) >= 0 "
        "AND CAST(unit_quantity AS NUMERIC) <= 1000000000)"
    ),
    "ck_model_cost_unit_estimated": (
        "unit IS NULL OR state != 'estimated' OR unit_quantity IS NOT NULL"
    ),
}


def upgrade() -> None:
    with op.batch_alter_table("model_cost") as batch:
        batch.add_column(sa.Column("unit", sa.String(16), nullable=True))
        batch.add_column(
            sa.Column(
                "unit_rate", sa.Numeric(24, 12).with_variant(sa.String(64), "sqlite"), nullable=True
            )
        )
        batch.add_column(
            sa.Column(
                "unit_maximum_quantity",
                sa.Numeric(24, 12).with_variant(sa.String(64), "sqlite"),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column(
                "unit_quantity",
                sa.Numeric(24, 12).with_variant(sa.String(64), "sqlite"),
                nullable=True,
            )
        )
        for name, expression in _CHECKS.items():
            batch.create_check_constraint(name, expression)


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("unit cost evidence cannot be checked offline; downgrade refused")
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM model_cost WHERE unit IS NOT NULL")):
        raise RuntimeError("unit cost evidence must be retained; downgrade refused")
    with op.batch_alter_table("model_cost") as batch:
        for name in _CHECKS:
            batch.drop_constraint(name, type_="check")
        for name in ("unit_quantity", "unit_maximum_quantity", "unit_rate", "unit"):
            batch.drop_column(name)
