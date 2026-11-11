"""迁移期兼容重导出：meetings 映射已迁至 app.meetings.persistence。"""

from app.meetings.persistence import MeetingActionClaimRecord, MeetingRecord

__all__ = ["MeetingActionClaimRecord", "MeetingRecord"]
