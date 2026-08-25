"""Backfill stable job fact slots for existing user memories.

Revision ID: 0018_memory_job_fact_key
Revises: 0017_proactive_delivery_receipts
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_memory_job_fact_key"
down_revision: str | None = "0017_proactive_delivery_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 只补空槽位; 人工维护或已结构化的 fact_key 永远优先。
    op.execute(
        sa.text(
            """
            UPDATE memory
            SET fact_key = 'profile.job'
            WHERE fact_key IS NULL
              AND subject_kind = 'user'
              AND (
                content LIKE '%工作是%'
                OR content LIKE '%工作为%'
                OR content LIKE '%职业是%'
                OR content LIKE '%职业为%'
                OR content LIKE '%从事%'
                OR content LIKE '%从业者%'
                OR content LIKE '%是%工程师%'
                OR content LIKE '%是%程序员%'
                OR content LIKE '%是%产品负责人%'
                OR content LIKE '%是%产品经理%'
                OR content LIKE '%是%设计师%'
                OR content LIKE '%是%教师%'
                OR content LIKE '%是%医生%'
                OR content LIKE '%担任%工程师%'
                OR content LIKE '%担任%程序员%'
                OR content LIKE '%担任%产品负责人%'
                OR content LIKE '%担任%产品经理%'
                OR content LIKE '%担任%设计师%'
                OR content LIKE '%担任%教师%'
                OR content LIKE '%担任%医生%'
              )
            """
        )
    )


def downgrade() -> None:
    # 数据迁移无法区分迁移补值与之后人工确认的同名槽位, 降级时保留事实数据。
    pass
