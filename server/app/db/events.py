"""迁移期兼容重导出：events 映射已迁至 app.bus.persistence。"""

from app.bus.persistence import (
    ConsumerInboxRecord,
    DeadLetterRecord,
    EventRecord,
    OutboxRecord,
)

__all__ = [
    "ConsumerInboxRecord",
    "DeadLetterRecord",
    "EventRecord",
    "OutboxRecord",
]
