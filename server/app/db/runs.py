"""迁移期兼容重导出：runs 映射已迁至 app.runs.persistence。"""

from app.runs.persistence import ModelReservationRecord, TaskRunEventRecord, TaskRunRecord

__all__ = [
    "ModelReservationRecord",
    "TaskRunEventRecord",
    "TaskRunRecord",
]
