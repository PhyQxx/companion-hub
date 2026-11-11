"""迁移期兼容重导出：calendar/contacts 映射已迁至两个领域 persistence 包。"""

from app.calendar.persistence import (
    CalendarEventRecord,
    CalendarOAuthStateRecord,
    CalendarOAuthTokenRecord,
)
from app.contacts.persistence import ContactRecord

__all__ = [
    "CalendarEventRecord",
    "CalendarOAuthStateRecord",
    "CalendarOAuthTokenRecord",
    "ContactRecord",
]
