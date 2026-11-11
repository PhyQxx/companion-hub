"""迁移期兼容重导出：activity_summary 映射已迁至 app.tasks.persistence。"""

from app.tasks.persistence import ActivitySummaryRecord

__all__ = [
    "ActivitySummaryRecord",
]
