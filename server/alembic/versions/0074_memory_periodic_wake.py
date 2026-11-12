"""memory periodic wake scheduling column

Revision ID: 0074_memory_periodic_wake
Revises: 0073_memory_lexical_index
Create Date: 2026-11-12

方案 B4（docs/记忆系统优化实施方案 §6）：周期事实（生日/每年纪念日）
临近自动唤醒。新增 memory.next_relevant_at（含索引），存量周期事实行
按与 app.memory.lifecycle_policy.periodic_wake_at 相同口径回填
（生日限定 *.birthday fact_key + 年月日字面；纪念日"每年M月D日"）。
"""

import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from alembic import op

revision: str = "0074_memory_periodic_wake"
down_revision: str | None = "0073_memory_lexical_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BIRTHDAY_DATE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_YEARLY_DATE = re.compile(r"每年(\d{1,2})月(\d{1,2})日")
WAKE_AHEAD_DAYS = 7
BATCH = 500


def _next_anniversary(month: int, day: int, now: datetime) -> datetime | None:
    for year in (now.year, now.year + 1):
        try:
            candidate = datetime(year, month, day, tzinfo=UTC)
        except ValueError:
            candidate = datetime(year, 3, 1, tzinfo=UTC) if (month, day) == (2, 29) else None
        if candidate is not None and candidate >= now:
            return candidate
    return None


def _wake_at(content: str, fact_key: str | None, now: datetime) -> datetime | None:
    monthly_day: tuple[int, int] | None = None
    if fact_key and fact_key.endswith(".birthday"):
        match = _BIRTHDAY_DATE.search(content or "")
        if match:
            monthly_day = (int(match.group(2)), int(match.group(3)))
    else:
        match = _YEARLY_DATE.search(content or "")
        if match:
            monthly_day = (int(match.group(1)), int(match.group(2)))
    if monthly_day is None:
        return None
    anniversary = _next_anniversary(monthly_day[0], monthly_day[1], now)
    if anniversary is None:
        return None
    wake = anniversary - timedelta(days=WAKE_AHEAD_DAYS)
    return wake if wake > now else now


def upgrade() -> None:
    op.add_column(
        "memory", sa.Column("next_relevant_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_memory_next_relevant_at", "memory", ["next_relevant_at"])
    bind = op.get_bind()
    now = datetime.now(UTC)
    rows = bind.execute(
        sa.text(
            "SELECT id, content, fact_key FROM memory"
            " WHERE fact_key LIKE '%.birthday' OR content LIKE '%每年%月%日%'"
        )
    ).fetchall()
    for start in range(0, len(rows), BATCH):
        updates = [
            {"id": row[0], "wake": _wake_at(row[1] or "", row[2], now)}
            for row in rows[start : start + BATCH]
        ]
        for update_params in updates:
            if update_params["wake"] is not None:
                bind.execute(
                    sa.text(
                        "UPDATE memory SET next_relevant_at = :wake WHERE id = :id"
                    ),
                    update_params,
                )


def downgrade() -> None:
    op.drop_index("ix_memory_next_relevant_at", table_name="memory")
    op.drop_column("memory", "next_relevant_at")
