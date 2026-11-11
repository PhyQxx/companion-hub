"""迁移期兼容重导出：workflows 映射已迁至 app.workflows.persistence。"""

from app.workflows.persistence import WorkflowDraftRecord, WorkflowRecord

__all__ = ["WorkflowDraftRecord", "WorkflowRecord"]
